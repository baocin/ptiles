"""
Roads reader for PTiles format (.roads.ptiles).

Decodes road segment records with delta-encoded OSM IDs, indexed road
class/surface, and optional intersection tables (v2+). Provides
RoadSegment dataclass and RoadsReader with get_in_cell, get_in_bounds,
nearest, nearest_n.
"""

from __future__ import annotations

import io
import logging
import math
import os
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import h3
import zstandard as zstd

from ptiles.codec import (
    ROAD_CLASS_REVERSE,
    SURFACE_REVERSE,
    decode_varint,
    zigzag_decode,
    decode_coordinates,
    decode_string_u16,
    decode_string_u8,
    decode_indexed_or_custom,
    read_header,
    read_index,
    binary_search_index,
    decompress_block,
    HEADER_SIZE,
)

logger = logging.getLogger("ptiles.roads")


@dataclass(frozen=True, slots=True)
class RoadSegment:
    osm_id: int
    road_class: str
    coords: tuple[tuple[float, float], ...]  # (lon, lat) pairs
    name: str | None = None
    ref_tag: str | None = None
    oneway: str | None = None
    speed_limit_kmh: int | None = None
    lanes: int | None = None
    surface: str | None = None
    bridge_tunnel: str | None = None


class IntersectionType(IntEnum):
    TRAFFIC_SIGNALS = 1
    STOP = 2
    GIVE_WAY = 3
    ROUNDABOUT = 4

    def delay_seconds(self) -> float:
        return {1: 20.0, 2: 4.0, 3: 3.0, 4: 2.0}[self.value]


@dataclass(frozen=True, slots=True)
class Intersection:
    lon_micro: int
    lat_micro: int
    intersection_type: IntersectionType


@dataclass(frozen=True, slots=True)
class NearestRoad:
    road: RoadSegment
    distance_meters: float
    snapped_lat: float
    snapped_lon: float
    segment_index: int
    along_fraction: float


def decode_road(data: bytes, offset: int, prev_osm_id: int) -> tuple[dict, int]:
    """Decode a road segment record. Returns (road_dict, bytes_consumed)."""
    pos = offset

    # OSM way ID (delta varint, NOT zigzag)
    delta, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + delta

    # Vertex count (uint16)
    vertex_count = struct.unpack_from("<H", data, pos)[0]
    pos += 2

    # First coordinate
    first_lon = struct.unpack_from("<i", data, pos)[0]
    first_lat = struct.unpack_from("<i", data, pos + 4)[0]
    pos += 8

    # Delta coordinates
    coords, consumed = decode_coordinates(data, pos, first_lon, first_lat, vertex_count)
    pos += consumed

    # Flags
    flags = data[pos]
    pos += 1

    # Road class
    road_class, consumed = decode_indexed_or_custom(data, pos, ROAD_CLASS_REVERSE)
    pos += consumed

    road: dict[str, Any] = {
        "osm_id": osm_id,
        "road_class": road_class,
        "coords": coords,
    }

    # Optional fields
    if flags & 0x01:
        road["name"], consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x02:
        road["ref_tag"], consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x04:
        ow = data[pos]
        pos += 1
        road["oneway"] = {0: "no", 1: "forward", 2: "reverse"}.get(ow, "no")
    if flags & 0x08:
        road["speed_limit_kmh"] = data[pos]
        pos += 1
    if flags & 0x10:
        road["lanes"] = data[pos]
        pos += 1
    if flags & 0x20:
        road["surface"], consumed = decode_indexed_or_custom(data, pos, SURFACE_REVERSE)
        pos += consumed
    if flags & 0x40:
        bt = data[pos]
        pos += 1
        road["bridge_tunnel"] = {0: None, 1: "bridge", 2: "tunnel"}.get(bt)

    return road, pos - offset


def decode_road_segment(data: bytes, offset: int, prev_osm_id: int
                         ) -> tuple[RoadSegment | None, int, int]:
    """Decode one road record into a RoadSegment. Returns (seg, consumed, new_prev_osm_id)."""
    try:
        d, consumed = decode_road(data, offset, prev_osm_id)
        seg = RoadSegment(
            osm_id=d["osm_id"],
            road_class=d["road_class"],
            coords=tuple(d["coords"]),
            name=d.get("name"),
            ref_tag=d.get("ref_tag"),
            oneway=d.get("oneway"),
            speed_limit_kmh=d.get("speed_limit_kmh"),
            lanes=d.get("lanes"),
            surface=d.get("surface"),
            bridge_tunnel=d.get("bridge_tunnel"),
        )
        return seg, consumed, d["osm_id"]
    except Exception as e:
        logger.warning("Failed to decode road at offset %d: %s", offset, e)
        return None, 0, prev_osm_id


def decode_intersection_table(data: bytes, pos: int) -> tuple[list[Intersection], int]:
    """Decode the intersection table (v2+)."""
    start = pos
    count = struct.unpack_from("<H", data, pos)[0]
    pos += 2
    intersections: list[Intersection] = []
    for _ in range(count):
        lon_micro = struct.unpack_from("<i", data, pos)[0]
        lat_micro = struct.unpack_from("<i", data, pos + 4)[0]
        int_type = IntersectionType(data[pos + 8])
        intersections.append(Intersection(
            lon_micro=lon_micro,
            lat_micro=lat_micro,
            intersection_type=int_type,
        ))
        pos += 9
    return intersections, pos - start


def decode_block(data: bytes, version: int) -> tuple[list[RoadSegment], list[Intersection]]:
    """Decode all road records and intersection table from a decompressed block."""
    roads: list[RoadSegment] = []
    pos = 0
    prev_osm_id = 0
    while pos < len(data) - 4:
        try:
            record_len = struct.unpack_from("<I", data, pos)[0]
        except struct.error:
            break
        if record_len == 0:
            pos += 4
            break
        pos += 4
        seg, consumed, prev_osm_id = decode_road_segment(data, pos, prev_osm_id)
        if seg is not None:
            roads.append(seg)
        pos += record_len

    intersections: list[Intersection] = []
    if version >= 2 and pos < len(data) - 2:
        ints, consumed = decode_intersection_table(data, pos)
        intersections.extend(ints)

    return roads, intersections


# --- Distance functions ---

def point_to_segment_distance_meters(
    px: float, py: float,  # query (lon, lat)
    ax: float, ay: float,  # segment start (lon, lat)
    bx: float, by: float,  # segment end (lon, lat)
) -> tuple[float, float, float, float]:
    """Compute planar point-to-segment distance with latitude scaling.

    Returns (distance_meters, snapped_lon, snapped_lat, along_fraction).
    """
    mean_lat = (py + ay + by) / 3.0
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * max(math.cos(math.radians(mean_lat)), 0.001)

    pxm = px * m_per_deg_lon
    pym = py * m_per_deg_lat
    axm = ax * m_per_deg_lon
    aym = ay * m_per_deg_lat
    bxm = bx * m_per_deg_lon
    bym = by * m_per_deg_lat

    dx = bxm - axm
    dy = bym - aym
    len_sq = dx * dx + dy * dy

    if len_sq < 1e-12:
        t = 0.0
    else:
        dot = (pxm - axm) * dx + (pym - aym) * dy
        t = max(0.0, min(1.0, dot / len_sq))

    sxm = axm + t * dx
    sym = aym + t * dy
    distance = math.hypot(pxm - sxm, pym - sym)

    snapped_lon = ax + t * (bx - ax)
    snapped_lat = ay + t * (by - ay)

    return distance, snapped_lon, snapped_lat, t


def point_to_linestring_distance_meters(
    px: float, py: float,
    coords: tuple[tuple[float, float], ...],
) -> tuple[float, float, float, int, float]:
    """Minimum distance from point to linestring in meters.

    Returns (min_dist, snapped_lon, snapped_lat, segment_index, along_fraction).
    """
    min_dist = float("inf")
    best_lon = py
    best_lat = px
    best_seg = 0
    best_t = 0.0

    for i in range(len(coords) - 1):
        dist, slon, slat, t = point_to_segment_distance_meters(
            px, py,
            coords[i][0], coords[i][1],
            coords[i + 1][0], coords[i + 1][1],
        )
        if dist < min_dist:
            min_dist = dist
            best_lon = slon
            best_lat = slat
            best_seg = i
            best_t = t

    return min_dist, best_lon, best_lat, best_seg, best_t


def profile_matches(profile: str | None, road_class: str) -> bool:
    """Check if a road class matches the given profile."""
    if profile is None:
        return True
    driving_classes = {
        "motorway", "motorway_link", "trunk", "trunk_link",
        "primary", "primary_link", "secondary", "tertiary", "tertiary_link",
    }
    cycling_classes = driving_classes | {"cycleway"}
    if profile == "driving":
        return road_class in driving_classes
    if profile == "walking":
        return True
    if profile == "cycling":
        return road_class in cycling_classes
    return True


class RoadsReader:
    """Reader for .roads.ptiles files."""

    def __init__(self, f: io.BufferedReader, filepath: str):
        self._file = f
        self._filepath = filepath
        self._header = read_header(f)
        self._version = self._header.get("version", 1)

        # Read zstd dictionary
        f.seek(self._header["dict_offset"])
        self._dict_data = f.read(self._header["dict_length"])

        # Read spatial index
        f.seek(self._header["index_offset"])
        index_bytes = f.read(self._header["index_length"])
        self._index = read_index(index_bytes)

        # Detect relative offsets
        self._relative_offsets = True
        if self._index:
            first_off = self._index[0]["block_offset"]
            self._relative_offsets = first_off < self._header["blocks_offset"]

    @classmethod
    def open(cls, path: str | os.PathLike) -> "RoadsReader":
        """Open a .roads.ptiles file."""
        f = open(path, "rb")
        return cls(f, str(path))

    @property
    def header(self) -> dict:
        return self._header

    def _resolve_offset(self, offset: int) -> int:
        if self._relative_offsets:
            return self._header["blocks_offset"] + offset
        return offset

    def _read_block(self, cell_int: int) -> tuple[list[RoadSegment], list[Intersection]]:
        """Read and decode a block for a given H3 cell."""
        entry = binary_search_index(self._index, cell_int)
        if entry is None:
            return [], []

        file_offset = self._resolve_offset(entry["block_offset"])
        self._file.seek(file_offset)
        compressed = self._file.read(entry["block_length"])

        raw = None
        if self._dict_data:
            try:
                raw = decompress_block(compressed, self._dict_data)
            except Exception:
                pass
        if raw is None:
            try:
                raw = zstd.ZstdDecompressor().decompress(compressed)
            except Exception as e:
                logger.warning("Decompress failed for cell %d: %s", cell_int, e)
                return [], []

        return decode_block(raw, self._version)

    def get_in_cell(self, cell: int | str) -> list[RoadSegment]:
        """Get all road segments in a single H3 res-7 cell."""
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        roads, _ = self._read_block(cell_int)
        return roads

    def get_cell_roads(self, cell: int | str) -> tuple[list[RoadSegment], list[Intersection]]:
        """Get all road segments + intersections in a single H3 res-7 cell."""
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        return self._read_block(cell_int)

    def get_in_bounds(self, min_lat: float, min_lon: float,
                      max_lat: float, max_lon: float,
                      limit: int = 1000) -> list[RoadSegment]:
        """Get all road segments within a lat/lon bounding box."""
        try:
            cells = h3.polygon_to_cells(
                [(min_lat, min_lon), (min_lat, max_lon),
                 (max_lat, max_lon), (max_lat, min_lon)],
                res=7,
            )
        except Exception:
            # Fallback: just use center cell
            center_lat = (min_lat + max_lat) / 2
            center_lon = (min_lon + max_lon) / 2
            center_cell = h3.latlng_to_cell(center_lat, center_lon, 7)
            cells = h3.grid_disk(center_cell, 2)

        seen: set[int] = set()
        results: list[RoadSegment] = []
        for cell in cells:
            cell_int = int(cell, 16) if isinstance(cell, str) else cell
            roads, _ = self._read_block(cell_int)
            for r in roads:
                if r.osm_id not in seen:
                    seen.add(r.osm_id)
                    results.append(r)
                    if len(results) >= limit:
                        return results
        return results

    def nearest(self, lat: float, lon: float, *,
                radius_meters: float = 100,
                profile: str | None = None,
                rings: int = 1) -> NearestRoad | None:
        """Find the nearest road to a point using H3 cell lookup.

        Inlines distance checks for performance — avoids intermediate
        collection and the point_to_linestring wrapper overhead.
        """
        cell = h3.latlng_to_cell(lat, lon, 7)
        _point_to_seg = point_to_segment_distance_meters

        best: NearestRoad | None = None
        best_dist = float('inf')

        neighbor_cells = h3.grid_disk(cell, rings)

        for neighbor in neighbor_cells:
            h3_int = int(neighbor, 16)
            roads, _ = self._read_block(h3_int)
            if not roads:
                continue

            for road in roads:
                if profile is not None and not profile_matches(profile, road.road_class):
                    continue

                coords = road.coords
                for i in range(len(coords) - 1):
                    d, slon, slat, frac = _point_to_seg(
                        lon, lat,
                        coords[i][0], coords[i][1],
                        coords[i + 1][0], coords[i + 1][1],
                    )
                    if d < best_dist and d <= radius_meters:
                        best_dist = d
                        best = NearestRoad(
                            road=road, distance_meters=d,
                            snapped_lat=slat, snapped_lon=slon,
                            segment_index=i, along_fraction=frac,
                        )

        return best

    def nearest_n(self, lat: float, lon: float, n: int = 5, *,
                  radius_meters: float = 100,
                  profile: str | None = None) -> list[NearestRoad]:
        """Find the N nearest roads to a point, ranked by distance."""
        cell = h3.latlng_to_cell(lat, lon, 7)
        neighbor_cells = h3.grid_disk(cell, 1)
        _point_to_seg = point_to_segment_distance_meters

        all_results: list[NearestRoad] = []
        seen_osm: set[int] = set()

        for neighbor in neighbor_cells:
            h3_int = int(neighbor, 16)
            roads, _ = self._read_block(h3_int)
            if not roads:
                continue
            for road in roads:
                if road.osm_id in seen_osm:
                    continue
                seen_osm.add(road.osm_id)
                if profile is not None and not profile_matches(profile, road.road_class):
                    continue

                coords = road.coords
                best_dist = float("inf")
                best_slon = 0.0
                best_slat = 0.0
                best_seg = 0
                best_t = 0.0
                for i in range(len(coords) - 1):
                    d, slon, slat, t = _point_to_seg(
                        lon, lat,
                        coords[i][0], coords[i][1],
                        coords[i + 1][0], coords[i + 1][1],
                    )
                    if d < best_dist:
                        best_dist = d
                        best_slon = slon
                        best_slat = slat
                        best_seg = i
                        best_t = t

                if best_dist <= radius_meters:
                    all_results.append(NearestRoad(
                        road=road,
                        distance_meters=round(best_dist, 1),
                        snapped_lat=best_slat,
                        snapped_lon=best_slon,
                        segment_index=best_seg,
                        along_fraction=best_t,
                    ))

        all_results.sort(key=lambda r: r.distance_meters)
        return all_results[:n]

    def close(self) -> None:
        """Close the underlying file."""
        self._file.close()
