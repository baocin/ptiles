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

from ptiles.geometry import (  # noqa: F401  (re-exported for callers)
    point_to_linestring_distance_meters,
    point_to_segment_distance_meters,
)
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
#
# Live in ptiles.geometry now; re-exported here because callers (and the
# router) import them from this module.


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

        # Dictionary and index are read on first use, not here: the index costs
        # roughly 1 KB of Python object per block, and a multi-country client
        # opens far more files than it queries. See ptiles/reader.py.
        self._index_cache = None
        self._dict_cache = None
        self._relative_offsets_cache = None

        self._block_cache: dict[int, tuple[list[RoadSegment], list[Intersection]]] = {}
        self._block_cache_max = 5000

    @property
    def _index(self):
        """Spatial index, decoded on first use -- see ptiles/reader.py."""
        if self._index_cache is None:
            self._file.seek(self._header["index_offset"])
            raw = self._file.read(self._header["index_length"])
            self._index_cache = read_index(raw)
        return self._index_cache

    @property
    def _dict_data(self):
        if self._dict_cache is None:
            self._file.seek(self._header["dict_offset"])
            self._dict_cache = self._file.read(self._header["dict_length"])
        return self._dict_cache

    @property
    def _relative_offsets(self):
        if self._relative_offsets_cache is None:
            idx = self._index
            self._relative_offsets_cache = (
                idx[0]["block_offset"] < self._header["blocks_offset"] if idx else True
            )
        return self._relative_offsets_cache

    @property
    def index_loaded(self):
        return self._index_cache is not None

    @classmethod
    def open(cls, path: str | os.PathLike):
        """Open a .roads.ptiles file.

        Two containers ship under this name: the original H3-indexed PTILESR,
        and PTLR, the three-zoom-band format every roads file has been built in
        since. Dispatch on the magic rather than making callers know which they
        have -- returns a PtlrRoadsReader for the latter.
        """
        f = open(path, "rb")
        magic = f.read(4)
        f.seek(0)
        if magic == b"PTLR":
            return PtlrRoadsReader(f, str(path))
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
        # Check block cache first
        if cell_int in self._block_cache:
            return self._block_cache[cell_int]

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

        roads, intersections = decode_block(raw, self._version)

        # Cache the result
        if len(self._block_cache) >= self._block_cache_max:
            self._block_cache.clear()
        self._block_cache[cell_int] = (roads, intersections)

        return roads, intersections

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
        # Precompute lat scale once for all segments (saves ~3 trig calls per segment)
        _m_per_deg_lon = 111320.0 * max(math.cos(math.radians(lat)), 0.001)

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
                        _m_per_deg_lon,
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
        _m_per_deg_lon = 111320.0 * max(math.cos(math.radians(lat)), 0.001)

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
                        _m_per_deg_lon,
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


# ===========================================================================
# PTLR — the three-zoom-band roads container
# ===========================================================================

# Reverse of scripts/build_roads.py ROAD_CLASS_INDEX. That map is many-to-one
# (motorway and motorway_link both encode as 0), so this names the canonical
# class per index; the link variant is not recoverable from the file.
PTLR_ROAD_CLASSES = [
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "service", "living_street", "track", "path", "pedestrian",
    "steps", "construction", "rest_area", "services",
]

PTLR_BANDS = ("z04", "z05", "z07")
_PTLR_BAND_HEADER = {"z04": 8, "z05": 24, "z07": 40}  # offset of each band's triple

# Index bucket size in micro-degrees: 0.01 deg, roughly 1.1 km. Buckets are
# small on purpose -- a point query decodes every road in the searched cells, so
# 5 km buckets meant tens of thousands of roads per query in central Tokyo.
_GRID = 1000


class PtlrRoadsReader:
    """Reader for PTLR roads files — three zoom bands, no spatial index.

    PTLR drops the H3 index the other layers carry: it is three ZSTD frames of
    concatenated road records, each frame a different simplification of the
    same road set. Nothing could read it from Python before, for any region
    including the US, so roads have been missing from the composite client
    since the format changed.

    With no index on disk, one is built in memory on first query by walking a
    band once and bucketing each road by the 0.05-degree cell of its first
    vertex. That walk is the expensive part -- it decodes every record's
    geometry, which for Japan's 10.5M roads takes a while and is why it is
    deferred until something actually asks.

    Consequence worth knowing: a road is bucketed by where it *starts*, so a
    long way whose first vertex lies outside the searched cells can be missed.
    `rings` widens the search; the default reaches roads starting within ~2 km,
    which is far outside any sane nearest-road radius but will not find a
    motorway whose geometry begins 50 km away. Raise `rings` for that, at
    proportional cost.

    Band choice: queries use Z05 (every road, simplified to 200 m) rather than
    Z07, since the extra vertices do not change which road is nearest at any
    scale a point query cares about, and Z07 is nearly twice the bytes.
    """

    DEFAULT_BAND = "z05"

    def __init__(self, f: io.BufferedReader, filepath: str):
        self._file = f
        self._filepath = filepath
        f.seek(0)
        head = f.read(HEADER_SIZE)
        if head[:4] != b"PTLR":
            raise ValueError(f"not a PTLR file: {filepath}")
        self._format_version = head[4]

        self._bands = {}
        for name, off in _PTLR_BAND_HEADER.items():
            band_off, comp, decomp = struct.unpack_from("<QII", head, off)
            self._bands[name] = {"offset": band_off, "comp": comp, "decomp": decomp}

        self._road_count = struct.unpack_from("<I", head, 56)[0]
        if self._format_version >= 2:
            self._dict_lens = struct.unpack_from("<III", head, 60)
            counts = struct.unpack_from("<III", head, 72)
            for name, count in zip(PTLR_BANDS, counts):
                self._bands[name]["count"] = count
            mnx, mny, mxx, mxy = struct.unpack_from("<iiii", head, 84)
            self._bounds = None if (mnx, mny, mxx, mxy) == (0, 0, 0, 0) else (
                mny / 100_000, mnx / 100_000, mxy / 100_000, mxx / 100_000
            )
        else:
            # v1 recorded neither dictionary lengths nor bounds. The lengths are
            # still recoverable: a trained zstd dictionary starts with its own
            # magic, so the boundaries can be found by scanning. That matters --
            # every roads file published before the v2 header is v1, so without
            # this the 51 US files stay unreadable until they are rebuilt.
            self._dict_lens = self._recover_v1_dict_lens()
            self._bounds = None

        self._band_cache: dict[str, bytes] = {}
        self._grid_cache: dict[str, dict] = {}

    _ZSTD_DICT_MAGIC = b"\x37\xa4\x30\xec"

    def _recover_v1_dict_lens(self) -> tuple[int, ...] | None:
        """Split a v1 file's concatenated dictionaries by their zstd magic.

        Returns None if the scan does not find exactly one dictionary per band,
        in which case the file really is unreadable and says so on use.
        """
        first_band = min(b["offset"] for b in self._bands.values())
        if first_band <= HEADER_SIZE:
            return None
        self._file.seek(HEADER_SIZE)
        blob = self._file.read(first_band - HEADER_SIZE)
        starts = [
            i for i in range(len(blob) - 4)
            if blob[i : i + 4] == self._ZSTD_DICT_MAGIC
        ]
        if len(starts) != len(PTLR_BANDS) or starts[0] != 0:
            return None
        ends = starts[1:] + [len(blob)]
        return tuple(e - s for s, e in zip(starts, ends))

    @classmethod
    def open(cls, path: str | os.PathLike) -> "PtlrRoadsReader":
        return cls(open(path, "rb"), str(path))

    @property
    def header(self) -> dict:
        """Header shaped like the PTILES one, so callers can treat them alike."""
        h = {
            "magic": b"PTLR",
            "version": self._format_version,
            "feature_count": self._road_count,
            "block_count": len(PTLR_BANDS),
        }
        if self._bounds:
            h["min_lat"], h["min_lon"], h["max_lat"], h["max_lon"] = self._bounds
        return h

    @property
    def index_loaded(self) -> bool:
        return bool(self._grid_cache)

    def _dict_for(self, band: str) -> bytes:
        if not self._dict_lens:
            raise ValueError(
                f"{self._filepath}: PTLR v{self._format_version} records no "
                "dictionary lengths and they could not be recovered by scanning "
                "for the zstd dictionary magic. Rebuild with build_roads.py."
            )
        start = HEADER_SIZE
        for name, length in zip(PTLR_BANDS, self._dict_lens):
            if name == band:
                self._file.seek(start)
                return self._file.read(length)
            start += length
        raise KeyError(band)

    def band_bytes(self, band: str = DEFAULT_BAND) -> bytes:
        """Decompressed records for one zoom band, cached."""
        if band not in self._band_cache:
            import zstandard as zstd

            meta = self._bands[band]
            self._file.seek(meta["offset"])
            compressed = self._file.read(meta["comp"])
            d = zstd.ZstdCompressionDict(self._dict_for(band))
            raw = zstd.ZstdDecompressor(dict_data=d).decompress(
                compressed, max_output_size=meta["decomp"]
            )
            self._band_cache[band] = raw
        return self._band_cache[band]

    def _decode_at(self, raw: bytes, pos: int, prev_osm_id: int):
        """Decode one record. Returns (RoadSegment, next_pos, osm_id)."""
        delta, consumed = decode_varint(raw, pos)
        pos += consumed
        osm_id = prev_osm_id + delta  # plain varint: PBF ways are id-ascending

        (count,) = struct.unpack_from("<H", raw, pos)
        pos += 2
        lon, lat = struct.unpack_from("<ii", raw, pos)
        pos += 8
        coords = [(lon / 100_000, lat / 100_000)]
        for _ in range(count - 1):
            dlon, consumed = decode_varint(raw, pos)
            pos += consumed
            dlat, consumed = decode_varint(raw, pos)
            pos += consumed
            lon += zigzag_decode(dlon)
            lat += zigzag_decode(dlat)
            coords.append((lon / 100_000, lat / 100_000))

        cls_idx = raw[pos]
        pos += 1
        flags = raw[pos]
        pos += 1
        name = ref = None
        if flags & 0x01:
            (n,) = struct.unpack_from("<H", raw, pos)
            pos += 2
            name = raw[pos : pos + n].decode("utf-8", "replace")
            pos += n
        if flags & 0x02:
            (n,) = struct.unpack_from("<H", raw, pos)
            pos += 2
            ref = raw[pos : pos + n].decode("utf-8", "replace")
            pos += n

        road = RoadSegment(
            osm_id=osm_id,
            road_class=(
                PTLR_ROAD_CLASSES[cls_idx] if cls_idx < len(PTLR_ROAD_CLASSES) else "unknown"
            ),
            coords=tuple(coords),
            name=name,
            ref_tag=ref,
        )
        return road, pos, osm_id

    def _grid(self, band: str = DEFAULT_BAND) -> dict:
        """Bucket every road by the 0.05-degree cell of its first vertex.

        Built once per band, on demand. See the class docstring for why this
        exists and what it costs.
        """
        if band in self._grid_cache:
            return self._grid_cache[band]
        raw = self.band_bytes(band)
        from array import array as _array

        grid: dict[tuple[int, int], "array"] = {}
        pos = 0
        prev = 0
        n = len(raw)
        while pos < n:
            start = pos
            try:
                delta, consumed = decode_varint(raw, pos)
                pos += consumed
                prev += delta
                (count,) = struct.unpack_from("<H", raw, pos)
                pos += 2
                lon, lat = struct.unpack_from("<ii", raw, pos)
                pos += 8
                for _ in range(2 * (count - 1)):  # skip the delta pairs
                    _, consumed = decode_varint(raw, pos)
                    pos += consumed
                pos += 1  # road class
                flags = raw[pos]
                pos += 1
                for bit in (0x01, 0x02):
                    if flags & bit:
                        (ln,) = struct.unpack_from("<H", raw, pos)
                        pos += 2 + ln
            except (IndexError, struct.error):
                break
            key = (lon // _GRID, lat // _GRID)
            bucket = grid.get(key)
            if bucket is None:
                bucket = grid[key] = _array("q")
            bucket.append(start)
        self._grid_cache[band] = grid
        logger.debug("%s: indexed %d cells from band %s", self._filepath, len(grid), band)
        return grid

    def _candidates(self, lat: float, lon: float, rings: int, band: str):
        """Roads in the cells around a point, decoded."""
        grid = self._grid(band)
        raw = self.band_bytes(band)
        gx, gy = int(lon * 100_000) // _GRID, int(lat * 100_000) // _GRID
        for dx in range(-rings, rings + 1):
            for dy in range(-rings, rings + 1):
                for offset in grid.get((gx + dx, gy + dy), ()):
                    try:
                        road, _, _ = self._decode_at(raw, offset, 0)
                    except (IndexError, struct.error):
                        continue
                    yield road

    def nearest(self, lat: float, lon: float, *, radius_meters: float = 100,
                profile: str | None = None, rings: int = 2,
                band: str = DEFAULT_BAND) -> NearestRoad | None:
        found = self.nearest_n(lat, lon, n=1, radius_meters=radius_meters,
                               rings=rings, band=band)
        return found[0] if found else None

    def nearest_n(self, lat: float, lon: float, n: int = 5, *,
                  radius_meters: float = 1000, profile: str | None = None,
                  rings: int = 2, band: str = DEFAULT_BAND) -> list[NearestRoad]:
        results: list[NearestRoad] = []
        for road in self._candidates(lat, lon, rings, band):
            best = None
            for i in range(len(road.coords) - 1):
                (x1, y1), (x2, y2) = road.coords[i], road.coords[i + 1]
                dist, sx, sy, frac = point_to_segment_distance_meters(
                    lon, lat, x1, y1, x2, y2
                )
                if best is None or dist < best[0]:
                    best = (dist, sx, sy, frac, i)
            if best is None or best[0] > radius_meters:
                continue
            dist, sx, sy, frac, idx = best
            results.append(
                NearestRoad(road=road, distance_meters=dist, snapped_lat=sy,
                            snapped_lon=sx, segment_index=idx, along_fraction=frac)
            )
        results.sort(key=lambda r: r.distance_meters)
        return results[:n]

    def get_in_bounds(self, min_lat: float, min_lon: float, max_lat: float,
                      max_lon: float, limit: int = 1000,
                      band: str = DEFAULT_BAND) -> list[RoadSegment]:
        grid = self._grid(band)
        raw = self.band_bytes(band)
        out: list[RoadSegment] = []
        x0, x1 = int(min_lon * 100_000) // _GRID, int(max_lon * 100_000) // _GRID
        y0, y1 = int(min_lat * 100_000) // _GRID, int(max_lat * 100_000) // _GRID
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                for offset in grid.get((gx, gy), ()):
                    try:
                        road, _, _ = self._decode_at(raw, offset, 0)
                    except (IndexError, struct.error):
                        continue
                    out.append(road)
                    if len(out) >= limit:
                        return out
        return out

    def close(self) -> None:
        self._file.close()
