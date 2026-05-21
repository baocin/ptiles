"""
Places reader for PTiles format (.places.ptiles).

Decodes place records (cities, towns, hamlets, etc.) from OSM data.
Provides Place dataclass and PlacesReader with get_in_bounds.
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
    decode_string_u16,
    decode_string_u8,
    read_header,
    read_index,
    binary_search_index,
    decompress_block,
)

logger = logging.getLogger("ptiles.places")

PLACE_TYPE_REVERSE = {
    0: "city", 1: "town", 2: "village", 3: "hamlet",
    4: "neighborhood", 5: "suburb", 6: "borough", 7: "quarter",
    8: "isolated_dwelling",
}


@dataclass(frozen=True, slots=True)
class Place:
    osm_id: int
    lat: float
    lon: float
    place_type: str
    population: int
    name: str
    alt_name: str | None = None
    admin_level: int | None = None


def decode_place(data: bytes, offset: int, prev_osm_id: int) -> tuple[dict, int, int]:
    """Decode one place record. Returns (place_dict, bytes_consumed, new_prev_osm_id)."""
    pos = offset

    delta_raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(delta_raw)

    lon_micro = struct.unpack_from("<i", data, pos)[0]
    lat_micro = struct.unpack_from("<i", data, pos + 4)[0]
    pos += 8

    place_type_idx = data[pos]
    pos += 1
    place_type = PLACE_TYPE_REVERSE.get(place_type_idx, f"unknown({place_type_idx})")

    population_raw, consumed = decode_varint(data, pos)
    pos += consumed
    population = population_raw  # varint is unsigned

    name, consumed = decode_string_u16(data, pos)
    pos += consumed

    flags = data[pos]
    pos += 1

    alt_name = None
    admin_level = None
    if flags & 0x01:
        alt_name, consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x02:
        admin_level = data[pos]
        pos += 1

    return {
        "osm_id": osm_id,
        "lat": lat_micro / 100_000,
        "lon": lon_micro / 100_000,
        "place_type": place_type,
        "population": population,
        "name": name,
        "alt_name": alt_name,
        "admin_level": admin_level,
    }, pos - offset, osm_id


def decode_block(data: bytes) -> list[dict]:
    """Decode all place records from a decompressed block."""
    places: list[dict] = []
    pos = 0
    prev_osm_id = 0
    while pos < len(data):
        try:
            feat, consumed, prev_osm_id = decode_place(data, pos, prev_osm_id)
            places.append(feat)
            pos += consumed
        except (IndexError, struct.error):
            break
    return places


class PlacesReader:
    """Reader for .places.ptiles files."""

    def __init__(self, f: io.BufferedReader, filepath: str):
        self._file = f
        self._filepath = filepath
        self._header = read_header(f)
        self._version = self._header.get("version", 1)

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
    def open(cls, path: str | os.PathLike) -> "PlacesReader":
        f = open(path, "rb")
        return cls(f, str(path))

    @property
    def header(self) -> dict:
        return self._header

    def _resolve_offset(self, offset: int) -> int:
        if self._relative_offsets:
            return self._header["blocks_offset"] + offset
        return offset

    def _read_block(self, cell_int: int) -> list[Place]:
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
            Place(
                osm_id=d["osm_id"],
                lat=d["lat"],
                lon=d["lon"],
                place_type=d["place_type"],
                population=d["population"],
                name=d["name"],
                alt_name=d["alt_name"],
                admin_level=d["admin_level"],
            )
            for d in raw_dicts
        ]

    def get_in_cell(self, cell: int | str) -> list[Place]:
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        return self._read_block(cell_int)

    def get_in_bounds(self, min_lat: float, min_lon: float,
                      max_lat: float, max_lon: float,
                      limit: int = 1000) -> list[Place]:
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
        results: list[Place] = []
        for cell in cells:
            cell_int = int(cell, 16) if isinstance(cell, str) else cell
            for p in self._read_block(cell_int):
                if min_lat <= p.lat <= max_lat and min_lon <= p.lon <= max_lon:
                    results.append(p)
                    if len(results) >= limit:
                        return results
        return results

    def close(self) -> None:
        self._file.close()
