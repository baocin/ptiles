"""
Parks reader for PTiles format (.parks.ptiles).

Decodes park feature records (parks, nature reserves, playgrounds, etc.).
Provides ParkFeature dataclass and ParkReader with get_in_bounds.
"""

from __future__ import annotations

import io
import logging
import os
import struct
from dataclasses import dataclass

import h3
import zstandard as zstd

from ptiles.codec import (
    decode_varint,
    zigzag_decode,
    decode_coordinates,
    decode_string_u16,
    read_header,
    read_index,
    binary_search_index,
    decompress_block,
)

logger = logging.getLogger("ptiles.parks")

PARK_TYPE_REVERSE = {
    0: "park", 1: "nature_reserve", 2: "national_park", 3: "garden",
    4: "playground", 5: "recreation_ground", 6: "golf_course",
    7: "dog_park", 8: "forest", 9: "wood", 10: "cemetery",
    11: "sports_centre", 12: "pitch", 13: "swimming_pool", 14: "track",
}


@dataclass(frozen=True, slots=True)
class ParkFeature:
    osm_id: int
    park_type: str
    coords: tuple[tuple[float, float], ...]
    name: str | None = None


def decode_park(data: bytes, offset: int, prev_osm_id: int) -> tuple[dict, int, int]:
    pos = offset
    delta_raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(delta_raw)

    vertex_count = struct.unpack_from("<H", data, pos)[0]
    pos += 2
    first_lon = struct.unpack_from("<i", data, pos)[0]
    first_lat = struct.unpack_from("<i", data, pos + 4)[0]
    pos += 8
    coords_list, consumed = decode_coordinates(data, pos, first_lon, first_lat, vertex_count)
    pos += consumed

    park_type_idx = data[pos]
    pos += 1
    park_type = PARK_TYPE_REVERSE.get(park_type_idx, f"unknown({park_type_idx})")

    flags = data[pos]
    pos += 1

    name = None
    if flags & 0x01:
        name, consumed = decode_string_u16(data, pos)
        pos += consumed

    return {
        "osm_id": osm_id,
        "park_type": park_type,
        "coords": coords_list,
        "name": name,
    }, pos - offset, osm_id


def decode_block(data: bytes) -> list[dict]:
    features: list[dict] = []
    pos = 0
    prev_osm_id = 0
    while pos < len(data):
        try:
            feat, consumed, prev_osm_id = decode_park(data, pos, prev_osm_id)
            features.append(feat)
            pos += consumed
        except (IndexError, struct.error):
            break
    return features


class ParkReader:
    """Reader for .parks.ptiles files."""

    def __init__(self, f: io.BufferedReader, filepath: str):
        self._file = f
        self._filepath = filepath
        self._header = read_header(f)
        f.seek(self._header["dict_offset"])
        self._dict_data = f.read(self._header["dict_length"])
        f.seek(self._header["index_offset"])
        index_bytes = f.read(self._header["index_length"])
        self._index = read_index(index_bytes)
        self._relative_offsets = True
        if self._index:
            first_off = self._index[0]["block_offset"]
            self._relative_offsets = first_off < self._header["blocks_offset"]

    @classmethod
    def open(cls, path: str | os.PathLike) -> "ParkReader":
        f = open(path, "rb")
        return cls(f, str(path))

    @property
    def header(self) -> dict:
        return self._header

    def _resolve_offset(self, offset: int) -> int:
        if self._relative_offsets:
            return self._header["blocks_offset"] + offset
        return offset

    def _read_block(self, cell_int: int) -> list[ParkFeature]:
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
        raw_dicts = decode_block(raw)
        return [
            ParkFeature(
                osm_id=d["osm_id"],
                park_type=d["park_type"],
                coords=tuple(d["coords"]),
                name=d["name"],
            )
            for d in raw_dicts
        ]

    def get_in_cell(self, cell: int | str) -> list[ParkFeature]:
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        return self._read_block(cell_int)

    def get_in_bounds(self, min_lat: float, min_lon: float,
                      max_lat: float, max_lon: float,
                      limit: int = 1000) -> list[ParkFeature]:
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
        results: list[ParkFeature] = []
        for cell in cells:
            cell_int = int(cell, 16) if isinstance(cell, str) else cell
            results.extend(self._read_block(cell_int))
            if len(results) >= limit:
                return results
        return results

    def close(self) -> None:
        self._file.close()
