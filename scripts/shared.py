"""
Shared I/O and compression for PTiles format.

Encoding primitives live in encoding.py (pure, no I/O).
This module adds header/index read/write, zstd compression,
and domain-specific constants.
"""

import struct
import io
import zstandard as zstd

try:
    from encoding import *  # noqa: F401, F403
except ImportError:
    from scripts.encoding import *  # noqa: F401, F403


# ===========================================================================
# PTiles Header (256 bytes)
# ===========================================================================

HEADER_SIZE = 256
HEADER_STRUCT = struct.Struct("<7sB B 3x f f f f Q I Q I Q I Q Q I 172x")
# magic(7) + null(1) + version(1) + pad(3) + min_lat(4) + min_lon(4) + max_lat(4) + max_lon(4)
# + feature_count(8) + block_count(4) + dict_offset(8) + dict_length(4)
# + index_offset(8) + index_length(4) + blocks_offset(8) + aux_offset(8) + aux_length(4) + reserved(172)


def write_header(
    f: io.BufferedWriter,
    magic: bytes,
    version: int,
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    feature_count: int,
    block_count: int,
    dict_offset: int,
    dict_length: int,
    index_offset: int,
    index_length: int,
    blocks_offset: int,
    aux_offset: int = 0,
    aux_length: int = 0,
):
    """Write 256-byte PTiles header."""
    header = HEADER_STRUCT.pack(
        magic[:7],
        0,
        version,
        min_lat,
        min_lon,
        max_lat,
        max_lon,
        feature_count,
        block_count,
        dict_offset,
        dict_length,
        index_offset,
        index_length,
        blocks_offset,
        aux_offset,
        aux_length,
    )
    f.write(header)


def read_header(f: io.BufferedReader) -> dict:
    """Read and parse 256-byte PTiles header."""
    data = f.read(HEADER_SIZE)
    if len(data) < HEADER_SIZE:
        raise ValueError(f"File too small for header ({len(data)} bytes)")
    vals = HEADER_STRUCT.unpack(data)
    return {
        "magic": vals[0],
        "version": vals[2],
        "min_lat": vals[3],
        "min_lon": vals[4],
        "max_lat": vals[5],
        "max_lon": vals[6],
        "feature_count": vals[7],
        "block_count": vals[8],
        "dict_offset": vals[9],
        "dict_length": vals[10],
        "index_offset": vals[11],
        "index_length": vals[12],
        "blocks_offset": vals[13],
        "aux_offset": vals[14],
        "aux_length": vals[15],
    }


# ===========================================================================
# Spatial Index
# ===========================================================================

INDEX_ENTRY_SIZE = 19  # 8 (h3_cell) + 6 (offset) + 3 (length) + 2 (count)


def encode_index_entry(
    h3_cell: int, block_offset: int, block_length: int, feature_count: int
) -> bytes:
    buf = struct.pack("<Q", h3_cell)
    buf += block_offset.to_bytes(6, "little")
    buf += block_length.to_bytes(3, "little")
    buf += struct.pack("<H", min(feature_count, 65535))
    return buf


def decode_index_entry(data: bytes, pos: int) -> dict:
    h3_cell = struct.unpack_from("<Q", data, pos)[0]
    block_offset = int.from_bytes(data[pos + 8 : pos + 14], "little")
    block_length = int.from_bytes(data[pos + 14 : pos + 17], "little")
    feature_count = struct.unpack_from("<H", data, pos + 17)[0]
    return {
        "h3_cell": h3_cell,
        "block_offset": block_offset,
        "block_length": block_length,
        "feature_count": feature_count,
    }


def write_index(f: io.BufferedWriter, entries: list[dict]):
    f.write(struct.pack("<I", len(entries)))
    for e in entries:
        f.write(
            encode_index_entry(
                e["h3_cell"], e["block_offset"], e["block_length"], e["feature_count"]
            )
        )


def read_index(data: bytes) -> list[dict]:
    entry_count = struct.unpack_from("<I", data, 0)[0]
    entries = []
    pos = 4
    for _ in range(entry_count):
        entries.append(decode_index_entry(data, pos))
        pos += INDEX_ENTRY_SIZE
    return entries


def binary_search_index(index: list[dict], h3_cell: int) -> dict | None:
    left, right = 0, len(index) - 1
    while left <= right:
        mid = (left + right) // 2
        mid_cell = index[mid]["h3_cell"]
        if mid_cell == h3_cell:
            return index[mid]
        elif mid_cell < h3_cell:
            left = mid + 1
        else:
            right = mid - 1
    return None


# ===========================================================================
# Zstd Compression
# ===========================================================================


def train_dictionary(samples: list[bytes], dict_size: int = 512 * 1024) -> bytes:
    return zstd.train_dictionary(dict_size, samples).as_bytes()


def compress_block(data: bytes, dict_data: bytes, level: int = 12) -> bytes:
    d = zstd.ZstdCompressionDict(dict_data)
    cctx = zstd.ZstdCompressor(level=level, dict_data=d)
    return cctx.compress(data)


def decompress_block(data: bytes, dict_data: bytes) -> bytes:
    d = zstd.ZstdCompressionDict(dict_data)
    dctx = zstd.ZstdDecompressor(dict_data=d)
    return dctx.decompress(data)


# ===========================================================================
# Building Type Index (v6, extended v8)
# ===========================================================================

BTYPE_INDEX = [
    "yes",
    "house",
    "residential",
    "commercial",
    "industrial",
    "retail",
    "garage",
    "apartments",
    "office",
    "warehouse",
    "shed",
    "detached",
    "terrace",
    "school",
    "church",
    "hospital",
    "hotel",
    "roof",
    "construction",
    "barn",
]

BTYPE_REVERSE = {i: t for i, t in enumerate(BTYPE_INDEX)}

USE_MAP = {
    "house": 1,
    "residential": 1,
    "detached": 1,
    "terrace": 1,
    "apartments": 1,
    "commercial": 2,
    "retail": 2,
    "office": 2,
    "warehouse": 2,
    "hotel": 2,
    "industrial": 3,
    "school": 3,
    "church": 3,
    "hospital": 3,
    "public": 3,
}

USE_REVERSE = {1: "residential", 2: "commercial", 3: "industrial/institutional"}


# ===========================================================================
# v2 Format Support (index entries, merged blocks, u16 coords)
# ===========================================================================

INDEX_ENTRY_SIZE_V2 = 38  # bytes per v2 index entry


def index_entry_v2_format():
    """Return struct format string for v2 index entry."""
    return "<QiiiiIHH"  # cell(8) + bbox(4*4) + block_offset_packed(6) + block_len(3) + cell_index(2)


def encode_index_entry_v2(
    h3_cell: int,
    min_lon: int,
    min_lat: int,
    max_lon: int,
    max_lat: int,
    block_offset: int,
    block_length: int,
    feature_count: int,
    cell_index: int,
) -> bytes:
    offset_hi = (block_offset >> 48) & 0xFFFF
    offset_lo = block_offset & 0xFFFFFFFFFFFF
    len_hi = (block_length >> 16) & 0xFF
    len_lo = block_length & 0xFFFF
    packed = struct.pack("<Qiiii", h3_cell, min_lon, min_lat, max_lon, max_lat)
    packed += offset_lo.to_bytes(6, "little")
    packed += struct.pack("<H", len_lo)
    packed += struct.pack("<BBH", offset_hi, len_hi, feature_count)
    packed += struct.pack("<H", cell_index)
    return packed


def encode_merged_block(
    cells: list[tuple[int, list[bytes]]], center_lon_micro: int, center_lat_micro: int
) -> bytes:
    sorted_cells = sorted(cells, key=lambda c: c[0])
    record_data = bytearray()
    offsets: list[int] = []
    for _, records in sorted_cells:
        offsets.append(len(record_data))
        for rec in records:
            record_data.extend(rec)
    buf = bytearray()
    buf.extend(
        struct.pack("<iiI", center_lon_micro, center_lat_micro, len(sorted_cells))
    )
    for (cell_id, _), off in zip(sorted_cells, offsets):
        buf.extend(struct.pack("<QI", cell_id, off))
    buf.extend(record_data)
    return bytes(buf)


def decode_merged_block(data: bytes) -> dict:
    center_lon_micro, center_lat_micro, cell_count = struct.unpack_from("<iiI", data, 0)
    pos = 12
    cell_entries = []
    for _ in range(cell_count):
        cell_id, off = struct.unpack_from("<QI", data, pos)
        cell_entries.append((cell_id, off))
        pos += 12
    record_data_start = pos
    cells = []
    for i, (cell_id, off) in enumerate(cell_entries):
        abs_start = record_data_start + off
        abs_end = (
            record_data_start + cell_entries[i + 1][1]
            if i + 1 < cell_count
            else len(data)
        )
        records: list[bytes] = []
        p = abs_start
        while p < abs_end:
            rec_len = struct.unpack_from("<I", data, p)[0]
            p += 4
            records.append(bytes(data[p : p + rec_len]))
            p += rec_len
        cells.append((cell_id, records))
    return {
        "center_lon_micro": center_lon_micro,
        "center_lat_micro": center_lat_micro,
        "cell_count": cell_count,
        "cells": cells,
    }


def encode_coords_u16(
    coords: list[tuple[float, float]], center_lon_micro: int, center_lat_micro: int
) -> bytes:
    buf = bytearray()
    prev_lon_micro = 0
    prev_lat_micro = 0
    for i, (lon, lat) in enumerate(coords):
        lon_micro = round(lon * 100_000)
        lat_micro = round(lat * 100_000)
        if i == 0:
            ux = (lon_micro - center_lon_micro) + 32768
            uy = (lat_micro - center_lat_micro) + 32768
            buf.extend(struct.pack("<HH", ux, uy))
        else:
            dx = lon_micro - prev_lon_micro
            dy = lat_micro - prev_lat_micro
            buf.extend(encode_varint(zigzag_encode(dx)))
            buf.extend(encode_varint(zigzag_encode(dy)))
        prev_lon_micro = lon_micro
        prev_lat_micro = lat_micro
    return bytes(buf)


def decode_coords_u16(
    data: bytes,
    pos: int,
    center_lon_micro: int,
    center_lat_micro: int,
    vertex_count: int,
) -> tuple[list[tuple[float, float]], int]:
    start_pos = pos
    coords: list[tuple[float, float]] = []
    prev_lon_micro = 0
    prev_lat_micro = 0
    for i in range(vertex_count):
        if i == 0:
            ux, uy = struct.unpack_from("<HH", data, pos)
            pos += 4
            lon_micro = (ux - 32768) + center_lon_micro
            lat_micro = (uy - 32768) + center_lat_micro
        else:
            dx_raw, consumed = decode_varint(data, pos)
            pos += consumed
            dy_raw, consumed = decode_varint(data, pos)
            pos += consumed
            lon_micro = prev_lon_micro + zigzag_decode(dx_raw)
            lat_micro = prev_lat_micro + zigzag_decode(dy_raw)
        coords.append((lon_micro / 100_000, lat_micro / 100_000))
        prev_lon_micro = lon_micro
        prev_lat_micro = lat_micro
    return coords, pos - start_pos
