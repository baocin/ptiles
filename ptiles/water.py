"""
Water reader for PTiles format (.water.ptiles).

Decodes water feature records (polygons, linestrings, references) and
the large-water-body aux table. Provides WaterFeature, LargeWaterBody
dataclasses and WaterReader with get_in_cell, get_in_bounds,
large_water_bodies.
"""

from __future__ import annotations

import io
import logging
import math
import os
import struct
from dataclasses import dataclass
from enum import IntEnum

import h3
import zstandard as zstd

from ptiles.codec import (
    WATER_TYPES,
    decode_varint,
    zigzag_decode,
    decode_string_u16,
    decode_string_u8,
    decode_indexed_or_custom,
    read_header,
    read_index,
    binary_search_index,
    decompress_block,
    HEADER_SIZE,
)

logger = logging.getLogger("ptiles.water")


class GeomType(IntEnum):
    POLYGON = 0
    LINESTRING = 1
    REFERENCE = 2


@dataclass(frozen=True, slots=True)
class WaterFeature:
    osm_id: int
    water_type: str
    geom_type: GeomType
    coords: tuple[tuple[float, float], ...]  # (lon, lat) pairs
    ref_feature_id: int | None = None
    name: str | None = None
    width: int | None = None


@dataclass(frozen=True, slots=True)
class LargeWaterBody:
    feature_id: int
    name: str
    water_type: str
    coords: tuple[tuple[float, float], ...]


def decode_water_record(data: bytes, pos: int, prev_osm_id: int) -> tuple[dict, int, int]:
    """Decode one water feature record. Returns (feature_dict, bytes_consumed, new_prev_osm_id)."""
    start = pos

    # osm_id: varint delta (zigzag)
    delta_raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(delta_raw)

    # geom_type: u8
    geom_type = data[pos]
    pos += 1

    coords: list[tuple[float, float]] = []
    ref_feature_id = None

    if geom_type == 2:
        # Reference
        ref_feature_id = struct.unpack_from("<I", data, pos)[0]
        pos += 4
    else:
        # vertex_count: u16
        vertex_count = struct.unpack_from("<H", data, pos)[0]
        pos += 2

        if vertex_count > 0:
            first_lon = struct.unpack_from("<i", data, pos)[0]
            first_lat = struct.unpack_from("<i", data, pos + 4)[0]
            pos += 8

            coords = [(first_lon / 100_000, first_lat / 100_000)]
            prev_lon, prev_lat = first_lon, first_lat

            for _ in range(vertex_count - 1):
                dlon_raw, consumed = decode_varint(data, pos)
                pos += consumed
                dlat_raw, consumed = decode_varint(data, pos)
                pos += consumed
                prev_lon += zigzag_decode(dlon_raw)
                prev_lat += zigzag_decode(dlat_raw)
                coords.append((prev_lon / 100_000, prev_lat / 100_000))

    # flags: u8
    flags = data[pos]
    pos += 1

    # water_type: u8
    wt = data[pos]
    pos += 1
    water_type = WATER_TYPES[wt] if wt < len(WATER_TYPES) else f"unknown({wt})"

    name = None
    width = None

    if flags & 0x01:
        name, consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x02:
        width = struct.unpack_from("<H", data, pos)[0]
        pos += 2
    if flags & 0x04:
        pos += 2  # skip depth

    geom_names = {0: "polygon", 1: "linestring", 2: "reference"}

    return {
        "osm_id": osm_id,
        "geom_type": geom_names.get(geom_type, f"unknown({geom_type})"),
        "water_type": water_type,
        "coords": coords,
        "ref_feature_id": ref_feature_id,
        "name": name,
        "width": width,
        "vertex_count": len(coords),
    }, pos - start, osm_id


def parse_large_water_body_table(data: bytes) -> list[LargeWaterBody]:
    """Parse the large water body feature table from aux data."""
    pos = 0
    count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    bodies: list[LargeWaterBody] = []
    for _ in range(count):
        feature_id = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        name, consumed = decode_string_u16(data, pos)
        pos += consumed
        wt = data[pos]
        pos += 1
        water_type = WATER_TYPES[wt] if wt < len(WATER_TYPES) else f"unknown({wt})"
        vertex_count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        first_lon = struct.unpack_from("<i", data, pos)[0]
        first_lat = struct.unpack_from("<i", data, pos + 4)[0]
        pos += 8
        coords = [(first_lon / 100_000, first_lat / 100_000)]
        prev_lon, prev_lat = first_lon, first_lat
        for _ in range(vertex_count - 1):
            dlon_raw, consumed = decode_varint(data, pos)
            pos += consumed
            dlat_raw, consumed = decode_varint(data, pos)
            pos += consumed
            prev_lon += zigzag_decode(dlon_raw)
            prev_lat += zigzag_decode(dlat_raw)
            coords.append((prev_lon / 100_000, prev_lat / 100_000))

        bodies.append(LargeWaterBody(
            feature_id=feature_id,
            name=name or "",
            water_type=water_type,
            coords=tuple(coords),
        ))
    return bodies


def decode_block(data: bytes) -> list[dict]:
    """Decode all water feature records from a decompressed block."""
    features: list[dict] = []
    pos = 0
    prev_osm_id = 0
    while pos < len(data):
        try:
            feat, consumed, prev_osm_id = decode_water_record(data, pos, prev_osm_id)
            features.append(feat)
            pos += consumed
        except (IndexError, struct.error):
            break
    return features


class WaterReader:
    """Reader for .water.ptiles files."""

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

        # Read large water body feature table from aux section
        self._feature_table: list[LargeWaterBody] = []
        if self._header["aux_offset"] > 0 and self._header["aux_length"] > 0:
            f.seek(self._header["aux_offset"])
            aux_data = f.read(self._header["aux_length"])
            try:
                decompressed = zstd.ZstdDecompressor().decompress(aux_data)
            except Exception:
                decompressed = aux_data
            try:
                self._feature_table = parse_large_water_body_table(decompressed)
            except Exception as e:
                logger.warning("Failed to parse large water body table: %s", e)

    @classmethod
    def open(cls, path: str | os.PathLike) -> "WaterReader":
        """Open a .water.ptiles file."""
        f = open(path, "rb")
        return cls(f, str(path))

    @property
    def header(self) -> dict:
        return self._header

    def _resolve_offset(self, offset: int) -> int:
        if self._relative_offsets:
            return self._header["blocks_offset"] + offset
        return offset

    def _read_block(self, cell_int: int) -> list[dict]:
        """Read and decode a block for a given H3 cell."""
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

        return decode_block(raw)

    def large_water_bodies(self) -> list[LargeWaterBody]:
        """Return the list of large water bodies from the aux section."""
        return list(self._feature_table)

    def get_in_cell(self, cell: int | str) -> list[WaterFeature]:
        """Get all water features in a single H3 res-7 cell.

        Resolves reference-type features using the large water body table.
        """
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        raw_features = self._read_block(cell_int)
        return list(self._resolve_features(raw_features))

    def get_in_bounds(self, min_lat: float, min_lon: float,
                      max_lat: float, max_lon: float,
                      limit: int = 1000) -> list[WaterFeature]:
        """Get all water features within a lat/lon bounding box."""
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

        seen: set[int] = set()
        results: list[WaterFeature] = []
        for cell in cells:
            cell_int = int(cell, 16) if isinstance(cell, str) else cell
            raw = self._read_block(cell_int)
            for feat in self._resolve_features(raw):
                if feat.osm_id not in seen:
                    seen.add(feat.osm_id)
                    results.append(feat)
                    if len(results) >= limit:
                        return results
        return results

    def _resolve_features(self, raw_features: list[dict]) -> list[WaterFeature]:
        """Convert raw dicts to WaterFeature dataclasses, resolving references."""
        results: list[WaterFeature] = []
        feature_table_map = {b.feature_id: b for b in self._feature_table}

        for f in raw_features:
            geom_type_val = 0 if f["geom_type"] == "polygon" else 1 if f["geom_type"] == "linestring" else 2
            coords_tuple = tuple(f["coords"])

            if f.get("ref_feature_id") is not None:
                ref_id = f["ref_feature_id"]
                if ref_id in feature_table_map:
                    wf = feature_table_map[ref_id]
                    coords_tuple = wf.coords

            results.append(WaterFeature(
                osm_id=f["osm_id"],
                water_type=f["water_type"],
                geom_type=GeomType(geom_type_val),
                coords=coords_tuple,
                ref_feature_id=f.get("ref_feature_id"),
                name=f.get("name"),
                width=f.get("width"),
            ))

        return results

    def close(self) -> None:
        """Close the underlying file."""
        self._file.close()
