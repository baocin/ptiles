"""
Buildings reader for PTiles format (.buildings_v8.ptiles).

Decodes building footprint records with v6 (zigzag deltas + record_len),
v7 (wall-segment encoding), and v8 (cell-relative first vertex +
per-cell string table) support.
"""

from __future__ import annotations

import io
import logging
import math
import os
import struct
from dataclasses import dataclass
from typing import Any

import h3
import zstandard as zstd

from ptiles.codec import (
    BTYPE_REVERSE,
    decode_varint,
    zigzag_decode,
    decode_coordinates,
    decode_string_u16,
    decode_string_u8,
    decode_string_table,
    decode_table_ref,
    read_header,
    read_index,
    binary_search_index,
    decompress_block,
    decode_indexed_or_custom,
    HEADER_SIZE,
    coord_to_micro,
)

logger = logging.getLogger("ptiles.buildings")


USE_REVERSE_V8 = {0: "unknown", 1: "residential", 2: "commercial", 3: "industrial/institutional"}
HEIGHT_TIERS = {0: "unknown", 1: "1-2", 2: "3-5", 3: "6+"}


@dataclass(frozen=True, slots=True)
class Building:
    osm_id: int
    building_type: str
    centroid_lat: float
    centroid_lon: float
    coordinates: tuple[tuple[float, float], ...]  # (lon, lat) pairs
    name: str | None = None
    category: str | None = None
    name_source: str | None = None
    poi_osm_id: int | None = None


def compute_centroid(coords: tuple[tuple[float, float], ...]) -> tuple[float, float]:
    """Compute unweighted mean centroid from coordinate list."""
    n = len(coords)
    if n == 0:
        return (0.0, 0.0)
    lon_sum = sum(c[0] for c in coords)
    lat_sum = sum(c[1] for c in coords)
    return lon_sum / n, lat_sum / n


def decode_wall_segment(angle_byte: int, length_byte: int, prev_lat: float) -> tuple[float, float]:
    """Decode a wall-segment delta (v7+). Returns (delta_lon, delta_lat) in degrees."""
    bearing_rad = (angle_byte * 360.0 / 256.0) * math.pi / 180.0
    length_m = length_byte * 0.2
    delta_lat = (length_m * math.cos(bearing_rad)) / 111320.0
    delta_lon = (length_m * math.sin(bearing_rad)) / (111320.0 * math.cos(prev_lat * math.pi / 180.0))
    return delta_lon, delta_lat


def decode_building_v8(data: bytes, offset: int, prev_osm_id: int,
                       cell_center_lon: float, cell_center_lat: float,
                       string_table: list[str]) -> tuple[Building | None, int, int]:
    """Decode a single v8 building record.

    Returns (building, bytes_consumed, new_prev_osm_id).
    """
    pos = offset
    try:
        # 1. OSM ID delta (zigzag varint)
        osm_id_delta, consumed = decode_varint(data, pos)
        pos += consumed
        osm_id = prev_osm_id + zigzag_decode(osm_id_delta)

        # 2. Flags byte
        flags = data[pos]
        pos += 1
        vc_packed = (flags >> 4) & 0x0F

        # Vertex count
        if vc_packed == 0x0F:
            vertex_count = data[pos]
            pos += 1
        else:
            vertex_count = vc_packed + 4

        # 3. Cell-relative first vertex + deltas
        coords_list: list[tuple[float, float]] = []
        if vertex_count > 0:
            offset_lon, offset_lat = struct.unpack_from("<hh", data, pos)
            pos += 4
            prev_lon = coord_to_micro(cell_center_lon) + offset_lon
            prev_lat = coord_to_micro(cell_center_lat) + offset_lat
            coords_list.append((prev_lon / 100_000.0, prev_lat / 100_000.0))

            for _ in range(vertex_count - 1):
                dlon_raw, consumed = decode_varint(data, pos)
                pos += consumed
                dlat_raw, consumed = decode_varint(data, pos)
                pos += consumed
                prev_lon += zigzag_decode(dlon_raw)
                prev_lat += zigzag_decode(dlat_raw)
                coords_list.append((prev_lon / 100_000.0, prev_lat / 100_000.0))

        # 4. Building type (table ref)
        btype_idx = data[pos]
        pos += 1
        if btype_idx == 0xff:
            btype, consumed = decode_string_u8(data, pos)
            pos += consumed
        elif btype_idx < len(string_table):
            btype = string_table[btype_idx]
        else:
            btype = "yes"

        # 5. Extended flags
        flags2 = data[pos]
        pos += 1

        name = None
        category = None
        name_source = None
        poi_osm_id = None

        if flags2 & 0x01:  # has_name
            name, consumed = decode_table_ref(data, pos, string_table)
            pos += consumed
        if flags2 & 0x02:  # has_category
            cat, consumed = decode_table_ref(data, pos, string_table)
            category = cat
            pos += consumed
        if flags2 & 0x04:  # has_name_source
            src, consumed = decode_table_ref(data, pos, string_table)
            name_source = src
            pos += consumed
        if flags2 & 0x08:  # has_poi_osm_id
            poi_osm_id = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
        # flags2 & 0x10: has_height_m (skip)

        coords_tuple = tuple(coords_list)
        centroid_lon, centroid_lat = compute_centroid(coords_tuple)

        building = Building(
            osm_id=osm_id,
            building_type=btype,
            centroid_lat=centroid_lat,
            centroid_lon=centroid_lon,
            coordinates=coords_tuple,
            name=name or None,
            category=category or None,
            name_source=name_source or None,
            poi_osm_id=poi_osm_id,
        )

        return building, pos - offset, osm_id

    except Exception as e:
        logger.warning("Failed to decode v8 building record at offset %d: %s", offset, e)
        return None, pos - offset, prev_osm_id if pos > offset else 0


def decode_building_v6(data: bytes, offset: int, prev_osm_id: int,
                       version: int) -> tuple[Building | None, int, int]:
    """Decode one building record (v6/v7 format)."""
    pos = offset

    try:
        # osm_id: varint delta from prev
        delta_raw, consumed = decode_varint(data, pos)
        pos += consumed
        osm_id = prev_osm_id + delta_raw

        # vertex count (u8)
        vertex_count = data[pos]
        pos += 1

        # first absolute vertex (i32 lon, i32 lat)
        first_lon = struct.unpack_from("<i", data, pos)[0]
        first_lat = struct.unpack_from("<i", data, pos + 4)[0]
        pos += 8

        if version >= 7:
            # Wall-segment encoding
            coords_list = [(first_lon / 100_000.0, first_lat / 100_000.0)]
            prev_lat_deg = first_lat / 100_000.0
            for _ in range(vertex_count - 1):
                angle_byte = data[pos]
                length_byte = data[pos + 1]
                pos += 2
                dlon, dlat = decode_wall_segment(angle_byte, length_byte, prev_lat_deg)
                new_lon = coords_list[-1][0] + dlon
                new_lat = coords_list[-1][1] + dlat
                coords_list.append((new_lon, new_lat))
                prev_lat_deg = new_lat
        else:
            # v6: zigzag-varint deltas
            coords_list, consumed = decode_coordinates(data, pos, first_lon, first_lat, vertex_count)
            pos += consumed

        # flags
        flags = data[pos]
        pos += 1

        # building type (indexed_or_custom)
        building_type, consumed = decode_indexed_or_custom(data, pos, BTYPE_REVERSE)
        pos += consumed

        name = None
        category = None
        name_source = None
        poi_osm_id = None

        if flags & 0x01:
            name, consumed = decode_string_u16(data, pos)
            pos += consumed
        if flags & 0x02:
            category, consumed = decode_string_u8(data, pos)
            pos += consumed
        if flags & 0x04:
            name_source, consumed = decode_string_u8(data, pos)
            pos += consumed
        if flags & 0x08:
            poi_osm_id = struct.unpack_from("<Q", data, pos)[0]
            pos += 8

        coords_tuple = tuple(coords_list)
        centroid_lon, centroid_lat = compute_centroid(coords_tuple)

        building = Building(
            osm_id=osm_id,
            building_type=building_type,
            centroid_lat=centroid_lat,
            centroid_lon=centroid_lon,
            coordinates=coords_tuple,
            name=name or None,
            category=category or None,
            name_source=name_source or None,
            poi_osm_id=poi_osm_id,
        )

        return building, pos - offset, osm_id

    except Exception as e:
        logger.warning("Failed to decode v6 building record at offset %d: %s", offset, e)
        return None, pos - offset, prev_osm_id if pos > offset else 0


def decode_v8_block(data: bytes, cell_center_lon: float, cell_center_lat: float) -> list[Building]:
    """Decode a v8 block: string table + u32-prefixed records."""
    # 1. Decode string table
    string_table, pos = decode_string_table(data, 0)

    # 2. Decode records — each with u32 length prefix
    buildings: list[Building] = []
    prev_osm_id = 0
    while pos < len(data) - 4:
        record_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if record_len == 0:
            break
        bldg, consumed, prev_osm_id = decode_building_v8(
            data, pos, prev_osm_id,
            cell_center_lon, cell_center_lat,
            string_table,
        )
        if bldg is not None:
            buildings.append(bldg)
        pos += record_len

    return buildings


class BuildingsReader:
    """Reader for .buildings_v8.ptiles files.

    Supports v6 (zigzag deltas), v7 (wall-segment), and v8 (cell-relative
    + per-cell string table) formats.
    """

    def __init__(self, f: io.BufferedReader, filepath: str):
        self._file = f
        self._filepath = filepath
        self._header = read_header(f)
        self._version = self._header["version"]

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
    def open(cls, path: str | os.PathLike) -> "BuildingsReader":
        """Open a .buildings_v8.ptiles file."""
        f = open(path, "rb")
        return cls(f, str(path))

    @property
    def header(self) -> dict:
        return self._header

    def _resolve_offset(self, offset: int) -> int:
        if self._relative_offsets:
            return self._header["blocks_offset"] + offset
        return offset

    def _get_cell_center(self, cell_int: int) -> tuple[float, float]:
        """Get (lon, lat) center of an H3 cell."""
        cell_hex = hex(cell_int)[2:]
        lat, lon = h3.cell_to_latlng(cell_hex)
        return lon, lat

    def _read_block(self, cell_int: int) -> list[Building]:
        """Read and decode a block for a given H3 cell."""
        if not self._index:
            # No spatial index — read single block at blocks_offset
            self._file.seek(self._header["blocks_offset"])
            compressed = self._file.read()
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
                    logger.warning("Decompress of single block failed: %s", e)
                    return []
            if self._version >= 8:
                center_lon, center_lat = self._get_cell_center(cell_int)
                return decode_v8_block(raw, center_lon, center_lat)
            else:
                return self._decode_v6_block(raw)

        entry = binary_search_index(self._index, cell_int)
        if entry is None:
            return []

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
                return []

        if self._version >= 8:
            center_lon, center_lat = self._get_cell_center(cell_int)
            return decode_v8_block(raw, center_lon, center_lat)
        else:
            return self._decode_v6_block(raw)

    def _decode_v6_block(self, raw: bytes) -> list[Building]:
        """Decode v6/v7 block format: {u32 record_len + record_body}*."""
        buildings: list[Building] = []
        pos = 0
        prev_osm_id = 0
        while pos < len(raw) - 4:
            try:
                record_len = struct.unpack_from("<I", raw, pos)[0]
            except struct.error:
                break
            if record_len == 0:
                break
            pos += 4
            bldg, consumed, prev_osm_id = decode_building_v6(raw, pos, prev_osm_id, self._version)
            if bldg is not None:
                buildings.append(bldg)
            pos += record_len
        return buildings

    def query(self, lat: float, lon: float) -> Building | None:
        """Find the building polygon containing (lat, lon).

        Searches the center cell first, then the 1-ring of neighbors.
        Returns the containing polygon, or the nearest within ~50 m.
        """
        center_cell = h3.latlng_to_cell(lat, lon, 7)
        cells_to_check = [center_cell]
        cells_to_check.extend(h3.grid_disk(center_cell, 1))
        cells_to_check = list(set(cells_to_check))

        all_buildings: list[Building] = []
        for cell in cells_to_check:
            if isinstance(cell, str):
                cell_int = int(cell, 16)
            else:
                cell_int = cell
            all_buildings.extend(self._read_block(cell_int))

        # Check for containment
        for bldg in all_buildings:
            if self._point_in_polygon(lon, lat, bldg.coordinates):
                return bldg

        # Nearest within 50 m
        nearest: Building | None = None
        nearest_dist: float = float("inf")
        for bldg in all_buildings:
            d = self._point_to_polygon_distance(lon, lat, bldg.coordinates)
            if d < nearest_dist and d <= 50.0:
                nearest_dist = d
                nearest = bldg
        return nearest

    def within(self, lat: float, lon: float, meters: float) -> list[Building]:
        """Find buildings whose footprint is within `meters` of (lat, lon)."""
        center_cell = h3.latlng_to_cell(lat, lon, 7)
        cells_to_check = list(set(h3.grid_disk(center_cell, 1)))

        results: list[Building] = []
        for cell in cells_to_check:
            cell_int = int(cell, 16) if isinstance(cell, str) else cell
            for bldg in self._read_block(cell_int):
                d = self._point_to_polygon_distance(lon, lat, bldg.coordinates)
                if d <= meters:
                    results.append(bldg)
        return results

    def get_in_cell(self, cell: int | str) -> list[Building]:
        """Get all buildings in a single H3 res-7 cell."""
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        return self._read_block(cell_int)

    def get_in_bounds(self, min_lat: float, min_lon: float,
                      max_lat: float, max_lon: float,
                      limit: int = 1000) -> list[Building]:
        """Get all buildings within a lat/lon bounding box."""
        try:
            cells = h3.polygon_to_cells(
                [(min_lat, min_lon), (min_lat, max_lon),
                 (max_lat, max_lon), (max_lat, min_lon)],
                res=7,
            )
        except Exception:
            center_lat = (min_lat + max_lat) / 2
            center_lon = (min_lon + max_lon) / 2
            center_cell = h3.latlng_to_cell(center_lat, center_lon, 7)
            cells = h3.grid_disk(center_cell, 2)

        results: list[Building] = []
        for cell in cells:
            cell_int = int(cell, 16) if isinstance(cell, str) else cell
            for bldg in self._read_block(cell_int):
                if min_lat <= bldg.centroid_lat <= max_lat and min_lon <= bldg.centroid_lon <= max_lon:
                    results.append(bldg)
                    if len(results) >= limit:
                        return results
        return results

    def close(self) -> None:
        """Close the underlying file."""
        self._file.close()

    # --- Geometry helpers ---

    @staticmethod
    def _point_in_polygon(px: float, py: float,
                          poly: tuple[tuple[float, float], ...]) -> bool:
        inside = False
        n = len(poly)
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > py) != (yj > py)) and \
               (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _point_to_segment_dist_sq(px: float, py: float,
                                   x1: float, y1: float,
                                   x2: float, y2: float) -> float:
        dx, dy = x2 - x1, y2 - y1
        len_sq = dx * dx + dy * dy
        if len_sq == 0.0:
            dx2, dy2 = px - x1, py - y1
            return dx2 * dx2 + dy2 * dy2
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
        proj_x = x1 + t * dx
        proj_y = y1 + t * dy
        dx2, dy2 = px - proj_x, py - proj_y
        return dx2 * dx2 + dy2 * dy2

    def _point_to_polygon_distance(self, px: float, py: float,
                                    poly: tuple[tuple[float, float], ...]) -> float:
        min_dist_sq = float("inf")
        for i in range(len(poly) - 1):
            d_sq = self._point_to_segment_dist_sq(
                px, py, poly[i][0], poly[i][1],
                poly[i + 1][0], poly[i + 1][1],
            )
            if d_sq < min_dist_sq:
                min_dist_sq = d_sq
        if len(poly) > 1:
            d_sq = self._point_to_segment_dist_sq(
                px, py, poly[-1][0], poly[-1][1],
                poly[0][0], poly[0][1],
            )
            if d_sq < min_dist_sq:
                min_dist_sq = d_sq
        return math.sqrt(min_dist_sq) * 111_000
