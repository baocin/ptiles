#!/usr/bin/env python3
"""
Read and query a .water.ptiles file.

Usage:
    python read_water.py <file.water.ptiles>                    # Show header info
    python read_water.py <file.water.ptiles> <lat> <lng>        # Point query
    python read_water.py <file.water.ptiles> bounds <s> <w> <n> <e>  # Bounds query
"""

import sys
import os
import struct
import json

import h3
import zstandard as zstd

sys.path.insert(0, os.path.dirname(__file__))
from shared import (
    read_header, read_index, binary_search_index,
    decode_varint, zigzag_decode, decode_string_u16,
)

WATER_TYPES = [
    "lake", "reservoir", "pond", "river", "stream",
    "creek", "canal", "drain", "bay", "ocean",
    "wetland", "marsh", "swamp", "estuary",
]


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

    coords = []
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


def query_water(ptiles_path: str, lat: float, lng: float):
    """Query water features for a GPS coordinate."""
    with open(ptiles_path, "rb") as f:
        header = read_header(f)

        # Read dictionary
        f.seek(header["dict_offset"])
        dict_data = f.read(header["dict_length"])

        # Read index
        f.seek(header["index_offset"])
        index_data = f.read(header["index_length"])
        index = read_index(index_data)

    cell = h3.latlng_to_cell(lat, lng, 7)
    cell_int = cell if isinstance(cell, int) else int(cell, 16)

    entry = binary_search_index(index, cell_int)
    if entry is None:
        return []

    # Read and decompress block
    with open(ptiles_path, "rb") as f:
        f.seek(entry["block_offset"])
        compressed = f.read(entry["block_length"])

    if dict_data:
        try:
            d = zstd.ZstdCompressionDict(dict_data)
            dctx = zstd.ZstdDecompressor(dict_data=d)
            data = dctx.decompress(compressed)
        except Exception:
            data = zstd.ZstdDecompressor().decompress(compressed)
    else:
        data = zstd.ZstdDecompressor().decompress(compressed)

    # Parse records
    features = []
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


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file.water.ptiles> [lat lng]")
        sys.exit(1)

    ptiles_path = sys.argv[1]

    with open(ptiles_path, "rb") as f:
        header = read_header(f)

    print(f"Format: Water")
    print(f"Version: {header['version']}")
    print(f"Bounds: ({header['min_lat']:.4f}, {header['min_lon']:.4f}) to ({header['max_lat']:.4f}, {header['max_lon']:.4f})")
    print(f"Features: {header['feature_count']:,}")
    print(f"Blocks: {header['block_count']:,}")
    print()

    if len(sys.argv) == 4:
        lat = float(sys.argv[2])
        lng = float(sys.argv[3])
        print(f"Query at ({lat}, {lng})...")
        features = query_water(ptiles_path, lat, lng)
        print(f"Found {len(features)} water features in cell")
        for f in features[:20]:
            name_str = f" ({f['name']})" if f['name'] else ""
            ref_str = f" -> ref#{f['ref_feature_id']}" if f['ref_feature_id'] else ""
            print(f"  {f['water_type']} {f['geom_type']}{name_str}{ref_str}: {f['vertex_count']} vertices")
    elif len(sys.argv) == 2:
        print("No query specified. Header info shown above.")
    else:
        print("Usage: read_water.py <file> [lat lng]")
