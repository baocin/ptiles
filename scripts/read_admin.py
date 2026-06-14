#!/usr/bin/env python3
"""
Read and query a US.admin.ptiles file.

Usage:
    python read_admin.py <file.admin.ptiles> <lat> <lng>
    python read_admin.py US.admin.ptiles 36.1627 -86.7816   # Nashville, TN

Returns state, county, ZIP code, and timezone for the query point.
"""

import sys
import os
import struct
import json

import h3
import zstandard as zstd

sys.path.insert(0, os.path.dirname(__file__))
from shared import read_header


# --- String Table Decoder ---

def decode_string_table(data: bytes, pos: int) -> tuple[list[str], int]:
    """Decode a string table. Returns (list_of_strings, bytes_consumed)."""
    start = pos
    count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    strings = []
    for _ in range(count):
        slen = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        s = data[pos:pos + slen].decode("utf-8")
        strings.append(s)
        pos += slen
    return strings, pos - start


def decode_all_string_tables(data: bytes) -> dict:
    """Decode all 5 string tables from compressed blob."""
    pos = 0
    country, consumed = decode_string_table(data, pos); pos += consumed
    state, consumed = decode_string_table(data, pos); pos += consumed
    county, consumed = decode_string_table(data, pos); pos += consumed
    zip_codes, consumed = decode_string_table(data, pos); pos += consumed
    tz, consumed = decode_string_table(data, pos); pos += consumed
    return {
        "country": country,
        "state": state,
        "county": county,
        "zip": zip_codes,
        "tz": tz,
    }


# --- Lookup Grid ---

GRID_ENTRY_SIZE = 16  # 8 + 1 + 1 + 2 + 2 + 1 + 1


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


# --- Query ---

def query_admin(ptiles_path: str, lat: float, lng: float) -> dict | None:
    """Query admin info for a GPS coordinate."""
    with open(ptiles_path, "rb") as f:
        header = read_header(f)

        # Read and decompress string tables
        f.seek(header["dict_offset"])
        st_compressed = f.read(header["dict_length"])
        dctx = zstd.ZstdDecompressor()
        st_data = dctx.decompress(st_compressed)
        string_tables = decode_all_string_tables(st_data)

        # Read lookup grid (auxiliary section, not compressed)
        f.seek(header["aux_offset"])
        grid_data = f.read(header["aux_length"])

    # Query
    cell = h3.latlng_to_cell(lat, lng, 7)
    cell_int = cell if isinstance(cell, int) else int(cell, 16)

    entry = binary_search_grid(grid_data, cell_int)
    if entry is None:
        return None

    result = {}

    if entry["country_idx"] < len(string_tables["country"]):
        result["country"] = string_tables["country"][entry["country_idx"]]

    if entry["state_idx"] < len(string_tables["state"]):
        result["state"] = string_tables["state"][entry["state_idx"]]

    if entry["county_idx"] < len(string_tables["county"]):
        result["county"] = string_tables["county"][entry["county_idx"]]

    if entry["zip_idx"] < len(string_tables["zip"]):
        result["zip"] = string_tables["zip"][entry["zip_idx"]]

    if entry["tz_idx"] < len(string_tables["tz"]):
        result["timezone"] = string_tables["tz"][entry["tz_idx"]]

    return result


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <file.admin.ptiles> <lat> <lng>")
        sys.exit(1)

    ptiles_path = sys.argv[1]
    lat = float(sys.argv[2])
    lng = float(sys.argv[3])

    print(f"Querying {ptiles_path} at ({lat}, {lng})...")
    result = query_admin(ptiles_path, lat, lng)

    if result is None:
        print("No admin data found (ocean or outside coverage)")
    else:
        print(json.dumps(result, indent=2))
