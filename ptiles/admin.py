"""
Admin reader for PTiles format (.admin.ptiles).

Decodes admin boundary information using a pre-computed lookup grid
(sorted by H3 res-7 cell) and string tables. Provides AdminInfo,
AdminPolygon dataclasses and AdminReader with query(), polygons().
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
    read_header,
    decode_string_u16,
)

logger = logging.getLogger("ptiles.admin")


@dataclass(frozen=True, slots=True)
class AdminInfo:
    country: str
    state: str
    county: str
    zip: str
    timezone: str
    boundary_flags: int


@dataclass(frozen=True, slots=True)
class AdminPolygon:
    name: str
    admin_level: int
    coordinates: tuple[tuple[float, float], ...]


GRID_ENTRY_SIZE = 16  # 8 + 1 + 1 + 2 + 2 + 1 + 1


def decode_string_table(data: bytes, pos: int) -> tuple[list[str], int]:
    """Decode a uint32-counted, uint16-strlen string table."""
    start = pos
    count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    strings: list[str] = []
    for _ in range(count):
        s, consumed = decode_string_u16(data, pos)
        strings.append(s)
        pos += consumed
    return strings, pos - start


def decode_all_string_tables(data: bytes) -> dict:
    """Decode all 5 string tables from compressed blob."""
    pos = 0
    country, consumed = decode_string_table(data, pos)
    pos += consumed
    state, consumed = decode_string_table(data, pos)
    pos += consumed
    county, consumed = decode_string_table(data, pos)
    pos += consumed
    zip_codes, consumed = decode_string_table(data, pos)
    pos += consumed
    tz, consumed = decode_string_table(data, pos)
    pos += consumed
    return {
        "country": country,
        "state": state,
        "county": county,
        "zip": zip_codes,
        "tz": tz,
    }


def decode_grid_entry(data: bytes, pos: int) -> dict:
    """Decode one grid entry at position."""
    h3_cell = struct.unpack_from("<Q", data, pos)[0]
    country_idx = data[pos + 8]
    state_idx = data[pos + 9]
    county_idx = struct.unpack_from("<H", data, pos + 10)[0]
    zip_idx = struct.unpack_from("<H", data, pos + 12)[0]
    tz_idx = data[pos + 14]
    boundary_flags = data[pos + 15]
    return {
        "h3_cell": h3_cell,
        "country_idx": country_idx,
        "state_idx": state_idx,
        "county_idx": county_idx,
        "zip_idx": zip_idx,
        "tz_idx": tz_idx,
        "boundary_flags": boundary_flags,
    }


def binary_search_grid(grid_data: bytes, cell_int: int) -> dict | None:
    """Binary search the lookup grid for an H3 cell."""
    entry_count = struct.unpack_from("<I", grid_data, 0)[0]
    left, right = 0, entry_count - 1

    while left <= right:
        mid = (left + right) // 2
        pos = 4 + mid * GRID_ENTRY_SIZE
        mid_cell = struct.unpack_from("<Q", grid_data, pos)[0]

        if mid_cell == cell_int:
            return decode_grid_entry(grid_data, pos)
        elif mid_cell < cell_int:
            left = mid + 1
        else:
            right = mid - 1

    return None


class AdminReader:
    """Reader for .admin.ptiles files."""

    def __init__(self, f: io.BufferedReader, filepath: str):
        self._file = f
        self._filepath = filepath
        self._header = read_header(f)

        # Read and decompress string tables (stored at dict_offset/dict_length)
        f.seek(self._header["dict_offset"])
        st_compressed = f.read(self._header["dict_length"])
        dctx = zstd.ZstdDecompressor()
        st_data = dctx.decompress(st_compressed)
        self._string_tables = decode_all_string_tables(st_data)

        # Read compressed polygon data (stored at index_offset/index_length)
        f.seek(self._header["index_offset"])
        self._polygons_compressed = f.read(self._header["index_length"])

        # Read lookup grid (stored at aux_offset/aux_length, uncompressed)
        f.seek(self._header["aux_offset"])
        self._grid_data = f.read(self._header["aux_length"])

    @classmethod
    def open(cls, path: str | os.PathLike) -> "AdminReader":
        """Open a .admin.ptiles file."""
        f = open(path, "rb")
        return cls(f, str(path))

    @property
    def header(self) -> dict:
        return self._header

    def query(self, lat: float, lon: float) -> AdminInfo | None:
        """Query admin info for a GPS coordinate."""
        cell = h3.latlng_to_cell(lat, lon, 7)
        if isinstance(cell, str):
            cell_int = int(cell, 16)
        else:
            cell_int = cell

        entry = binary_search_grid(self._grid_data, cell_int)
        if entry is None:
            return None

        st = self._string_tables
        country = st["country"][entry["country_idx"]] if entry["country_idx"] < len(st["country"]) else ""
        state = st["state"][entry["state_idx"]] if entry["state_idx"] < len(st["state"]) else ""
        county = st["county"][entry["county_idx"]] if entry["county_idx"] < len(st["county"]) else ""
        zip_code = st["zip"][entry["zip_idx"]] if entry["zip_idx"] < len(st["zip"]) else ""
        timezone = st["tz"][entry["tz_idx"]] if entry["tz_idx"] < len(st["tz"]) else ""

        return AdminInfo(
            country=country,
            state=state,
            county=county,
            zip=zip_code,
            timezone=timezone,
            boundary_flags=entry["boundary_flags"],
        )

    def polygons(self) -> list[AdminPolygon]:
        """Return admin boundary polygons (decompressed from index section)."""
        dctx = zstd.ZstdDecompressor()
        try:
            poly_data = dctx.decompress(self._polygons_compressed)
        except Exception:
            poly_data = self._polygons_compressed

        polygons: list[AdminPolygon] = []
        pos = 0
        count = struct.unpack_from("<I", poly_data, pos)[0]
        pos += 4

        for _ in range(count):
            state_idx = poly_data[pos]
            pos += 1
            name, consumed = decode_string_u16(poly_data, pos)
            pos += consumed
            vertex_count = struct.unpack_from("<I", poly_data, pos)[0]
            pos += 4

            coords: list[tuple[float, float]] = []
            for _ in range(vertex_count):
                lon = struct.unpack_from("<i", poly_data, pos)[0]
                lat = struct.unpack_from("<i", poly_data, pos + 4)[0]
                pos += 8
                coords.append((lon / 100_000, lat / 100_000))

            polygons.append(AdminPolygon(
                name=name,
                admin_level=4,
                coordinates=tuple(coords),
            ))

        return polygons

    def close(self) -> None:
        """Close the underlying file."""
        self._file.close()
