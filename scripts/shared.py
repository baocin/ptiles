"""
Shared encoding/decoding primitives for PTiles format.

Used by all layer builders and readers:
- Varint encoding (protobuf-style, 7 bits/byte)
- Zigzag encoding (signed → unsigned for small magnitudes)
- Coordinate delta encoding (microdegrees)
- PTiles header read/write (256 bytes)
- Spatial index read/write (H3 cell → block offset/length)
- Zstd dictionary training and compression
"""

import struct
import io
import zstandard as zstd


# --- Varint / Zigzag ---

def encode_varint(value: int) -> bytes:
    """Encode unsigned integer as varint (protobuf-style)."""
    buf = bytearray()
    while value >= 0x80:
        buf.append(0x80 | (value & 0x7F))
        value >>= 7
    buf.append(value)
    return bytes(buf)


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode unsigned varint. Returns (value, bytes_consumed)."""
    result = shift = 0
    start = pos
    while True:
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos - start


def zigzag_encode(n: int) -> int:
    """Encode signed integer as zigzag unsigned."""
    return (n << 1) ^ (n >> 63)


def zigzag_decode(n: int) -> int:
    """Decode zigzag unsigned to signed integer."""
    return (n >> 1) ^ -(n & 1)


# --- Coordinate Encoding ---

def coord_to_micro(deg: float) -> int:
    """Convert degrees to microdegrees (x100,000)."""
    return round(deg * 100_000)


def micro_to_coord(micro: int) -> float:
    """Convert microdegrees to degrees."""
    return micro / 100_000


def encode_coordinates(coords: list[tuple[float, float]]) -> tuple[bytes, int, int]:
    """Encode coordinate sequence as first absolute + zigzag varint deltas.

    Args:
        coords: List of (lon, lat) tuples in degrees.

    Returns:
        (delta_bytes, first_lon_micro, first_lat_micro)
    """
    first_lon = coord_to_micro(coords[0][0])
    first_lat = coord_to_micro(coords[0][1])

    buf = bytearray()
    prev_lon, prev_lat = first_lon, first_lat

    for lon, lat in coords[1:]:
        cur_lon = coord_to_micro(lon)
        cur_lat = coord_to_micro(lat)
        buf.extend(encode_varint(zigzag_encode(cur_lon - prev_lon)))
        buf.extend(encode_varint(zigzag_encode(cur_lat - prev_lat)))
        prev_lon, prev_lat = cur_lon, cur_lat

    return bytes(buf), first_lon, first_lat


def decode_coordinates(data: bytes, pos: int, first_lon: int, first_lat: int,
                       vertex_count: int) -> tuple[list[tuple[float, float]], int]:
    """Decode delta-encoded coordinate sequence.

    Returns:
        (coords_list, bytes_consumed)
    """
    coords = [(first_lon / 100_000, first_lat / 100_000)]
    prev_lon, prev_lat = first_lon, first_lat
    start_pos = pos

    for _ in range(vertex_count - 1):
        dlon_raw, consumed = decode_varint(data, pos)
        pos += consumed
        dlat_raw, consumed = decode_varint(data, pos)
        pos += consumed

        prev_lon += zigzag_decode(dlon_raw)
        prev_lat += zigzag_decode(dlat_raw)
        coords.append((prev_lon / 100_000, prev_lat / 100_000))

    return coords, pos - start_pos


# --- String Encoding ---

def encode_string_u16(s: str) -> bytes:
    """Encode string with uint16 length prefix."""
    encoded = s.encode("utf-8")
    return struct.pack("<H", len(encoded)) + encoded


def encode_string_u8(s: str) -> bytes:
    """Encode string with uint8 length prefix."""
    encoded = s.encode("utf-8")
    if len(encoded) > 255:
        encoded = encoded[:255]
    return struct.pack("B", len(encoded)) + encoded


def decode_string_u16(data: bytes, pos: int) -> tuple[str, int]:
    """Decode uint16-prefixed string. Returns (string, total_bytes_consumed)."""
    slen = struct.unpack_from("<H", data, pos)[0]
    s = data[pos + 2:pos + 2 + slen].decode("utf-8")
    return s, 2 + slen


def decode_string_u8(data: bytes, pos: int) -> tuple[str, int]:
    """Decode uint8-prefixed string. Returns (string, total_bytes_consumed)."""
    slen = data[pos]
    s = data[pos + 1:pos + 1 + slen].decode("utf-8")
    return s, 1 + slen


# --- PTiles Header (256 bytes) ---

HEADER_SIZE = 256
HEADER_STRUCT = struct.Struct("<7sB B 3x f f f f Q I Q I Q I Q Q I 172x")
# magic(7) + null(1) + version(1) + pad(3) + min_lat(4) + min_lon(4) + max_lat(4) + max_lon(4)
# + feature_count(8) + block_count(4) + dict_offset(8) + dict_length(4)
# + index_offset(8) + index_length(4) + blocks_offset(8) + aux_offset(8) + aux_length(4) + reserved(172)


def write_header(f: io.BufferedWriter, magic: bytes, version: int,
                 min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                 feature_count: int, block_count: int,
                 dict_offset: int, dict_length: int,
                 index_offset: int, index_length: int,
                 blocks_offset: int,
                 aux_offset: int = 0, aux_length: int = 0):
    """Write 256-byte PTiles header."""
    header = HEADER_STRUCT.pack(
        magic[:7], 0,  # magic + null terminator
        version,
        min_lat, min_lon, max_lat, max_lon,
        feature_count, block_count,
        dict_offset, dict_length,
        index_offset, index_length,
        blocks_offset,
        aux_offset, aux_length,
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


# --- Spatial Index ---

INDEX_ENTRY_SIZE = 19  # 8 (h3_cell) + 6 (offset) + 3 (length) + 2 (count)


def encode_index_entry(h3_cell: int, block_offset: int, block_length: int,
                       feature_count: int) -> bytes:
    """Encode one spatial index entry (19 bytes)."""
    buf = struct.pack("<Q", h3_cell)  # 8 bytes
    # 6-byte offset (little-endian)
    buf += block_offset.to_bytes(6, "little")
    # 3-byte length (little-endian)
    buf += block_length.to_bytes(3, "little")
    # 2-byte feature count
    buf += struct.pack("<H", min(feature_count, 65535))
    return buf


def decode_index_entry(data: bytes, pos: int) -> dict:
    """Decode one spatial index entry at position."""
    h3_cell = struct.unpack_from("<Q", data, pos)[0]
    block_offset = int.from_bytes(data[pos + 8:pos + 14], "little")
    block_length = int.from_bytes(data[pos + 14:pos + 17], "little")
    feature_count = struct.unpack_from("<H", data, pos + 17)[0]
    return {
        "h3_cell": h3_cell,
        "block_offset": block_offset,
        "block_length": block_length,
        "feature_count": feature_count,
    }


def write_index(f: io.BufferedWriter, entries: list[dict]):
    """Write spatial index: entry_count (4 bytes) + sorted entries."""
    f.write(struct.pack("<I", len(entries)))
    for e in entries:
        f.write(encode_index_entry(e["h3_cell"], e["block_offset"],
                                   e["block_length"], e["feature_count"]))


def read_index(data: bytes) -> list[dict]:
    """Parse spatial index from bytes."""
    entry_count = struct.unpack_from("<I", data, 0)[0]
    entries = []
    pos = 4
    for _ in range(entry_count):
        entries.append(decode_index_entry(data, pos))
        pos += INDEX_ENTRY_SIZE
    return entries


def binary_search_index(index: list[dict], h3_cell: int) -> dict | None:
    """Binary search the spatial index for an H3 cell."""
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


# --- Zstd Compression ---

def train_dictionary(samples: list[bytes], dict_size: int = 512 * 1024) -> bytes:
    """Train a zstd dictionary on sample data."""
    return zstd.train_dictionary(dict_size, samples).as_bytes()


def compress_block(data: bytes, dict_data: bytes, level: int = 12) -> bytes:
    """Compress a data block with zstd dictionary."""
    d = zstd.ZstdCompressionDict(dict_data)
    cctx = zstd.ZstdCompressor(level=level, dict_data=d)
    return cctx.compress(data)


def decompress_block(data: bytes, dict_data: bytes) -> bytes:
    """Decompress a data block with zstd dictionary."""
    d = zstd.ZstdCompressionDict(dict_data)
    dctx = zstd.ZstdDecompressor(dict_data=d)
    return dctx.decompress(data)


# --- Indexed Value Helpers ---

def encode_indexed_or_custom(value: str, index: dict[str, int]) -> bytes:
    """Encode a value as indexed byte or 255 + custom string."""
    if value in index:
        return struct.pack("B", index[value])
    return struct.pack("B", 255) + encode_string_u8(value)


def decode_indexed_or_custom(data: bytes, pos: int,
                             reverse_index: dict[int, str]) -> tuple[str, int]:
    """Decode indexed byte or 255 + custom string. Returns (value, bytes_consumed)."""
    idx = data[pos]
    if idx == 255:
        s, consumed = decode_string_u8(data, pos + 1)
        return s, 1 + consumed
    return reverse_index.get(idx, "unknown"), 1


# --- Per-Cell String Table (v8) ---

def build_string_table(strings: list[str]) -> tuple[list[str], dict[str, int]]:
    """Build a deduplicated string table for a block.

    Args:
        strings: All string values in the block (names, categories, etc.)

    Returns:
        (table, lookup) where table is list of unique strings and
        lookup maps string -> table index.
    """
    seen: dict[str, int] = {}
    table: list[str] = []
    for s in strings:
        if s not in seen:
            seen[s] = len(table)
            table.append(s)
    return table, seen


def encode_string_table(table: list[str]) -> bytes:
    """Encode string table for block header."""
    buf = bytearray()
    buf.append(min(len(table), 255))  # table size (u8)
    for s in table[:255]:
        buf.extend(encode_string_u8(s))
    return bytes(buf)


def decode_string_table(data: bytes, pos: int) -> tuple[list[str], int]:
    """Decode string table from block header.

    Returns:
        (table, bytes_consumed)
    """
    count = data[pos]
    pos += 1
    table = []
    for _ in range(count):
        s, consumed = decode_string_u8(data, pos)
        table.append(s)
        pos += consumed
    return table, pos


def encode_table_ref(value: str, table_lookup: dict[str, int]) -> bytes:
    """Encode a string as 1-byte table index, or 0xff + inline if not in table."""
    if value in table_lookup:
        idx = table_lookup[value]
        if idx <= 254:
            return struct.pack("B", idx)
    return struct.pack("B", 0xff) + encode_string_u8(value)


def decode_table_ref(data: bytes, pos: int,
                     table: list[str]) -> tuple[str, int]:
    """Decode a table-referenced string.

    Returns:
        (value, bytes_consumed)
    """
    idx = data[pos]
    pos += 1
    if idx == 0xff:
        s, consumed = decode_string_u8(data, pos)
        return s, 1 + consumed
    if idx < len(table):
        return table[idx], 1
    return "", 1


# --- Cell-Relative Coordinates (v8) ---

def encode_cell_relative(lon: float, lat: float, cell_center_lon: float,
                         cell_center_lat: float) -> bytes:
    """Encode first vertex as offset from H3 cell center.

    Offsets fit in i16 for H3 res >= 7 (cells < 6km wide).
    Returns 4 bytes (i16 lon, i16 lat) vs 8 bytes full int32.
    """
    offset_lon = coord_to_micro(lon) - coord_to_micro(cell_center_lon)
    offset_lat = coord_to_micro(lat) - coord_to_micro(cell_center_lat)
    return struct.pack("<hh", offset_lon, offset_lat)


def decode_cell_relative(data: bytes, pos: int, cell_center_lon: float,
                         cell_center_lat: float) -> tuple[float, float]:
    """Decode cell-relative first vertex.

    Returns:
        (lon, lat) in degrees
    """
    offset_lon, offset_lat = struct.unpack_from("<hh", data, pos)
    lon = (coord_to_micro(cell_center_lon) + offset_lon) / 100_000
    lat = (coord_to_micro(cell_center_lat) + offset_lat) / 100_000
    return lon, lat


# --- Vertex Count Bias (v8) ---

def encode_vertex_count(vc: int, extra_flags: int = 0) -> tuple[int, bytes | None]:
    """Encode vertex count with bias for common range (4-18).

    4-18 verts: packed into 4 bits (0-14), returned as extra_flags.
    <4 or >18: returns (flags with 0xF, raw_u8_bytes).

    Args:
        vc: Vertex count
        extra_flags: Existing extra flags to combine with

    Returns:
        (combined_flags, raw_bytes) where raw_bytes is None if packed into flags
    """
    if 4 <= vc <= 18:
        return (extra_flags | ((vc - 4) << 4)), None
    return (extra_flags | (0x0F << 4)), struct.pack("B", vc)


def decode_vertex_count(flags_byte: int, data: bytes | None = None,
                        pos: int = 0) -> tuple[int, int]:
    """Decode vertex count from flags byte or raw data.

    Sentinnel: 0x0F in top 4 bits = raw u8 follows.
    Otherwise: packed value + 4 = vertex count (range 4-18).

    Returns:
        (vertex_count, bytes_consumed_from_data)
    """
    packed = (flags_byte >> 4) & 0x0F
    if packed == 0x0F:
        # Sentinel: raw u8 follows
        return data[pos], 1
    return packed + 4, 0


# --- Building Type Index (v6, extended v8) ---

BTYPE_INDEX = [
    "yes", "house", "residential", "commercial", "industrial",
    "retail", "garage", "apartments", "office", "warehouse",
    "shed", "detached", "terrace", "school", "church",
    "hospital", "hotel", "roof", "construction", "barn",
]

BTYPE_REVERSE = {i: t for i, t in enumerate(BTYPE_INDEX)}

# Building use classification
USE_MAP = {
    "house": 1, "residential": 1, "detached": 1, "terrace": 1, "apartments": 1,
    "commercial": 2, "retail": 2, "office": 2, "warehouse": 2, "hotel": 2,
    "industrial": 3, "school": 3, "church": 3, "hospital": 3, "public": 3,
}

USE_REVERSE = {1: "residential", 2: "commercial", 3: "industrial/institutional"}


# ===========================================================================
# v2 Format Support (SPEC_v2.md)
# ===========================================================================
#
# v2 additions:
#   - Merged blocks (multiple sparse H3 cells share one zstd block)
#   - u16 cell-relative coordinates (saves bytes vs i32)
#   - 37-byte index entries with per-cell bbox + cell_index_in_block
#
# v1 functions above are unchanged for backward compat. v2 readers MUST
# read v1 files; v1 readers ignore v2 files (different header.version).

INDEX_ENTRY_SIZE_V2 = 37  # 8 + 4*4 + 6 + 3 + 2 + 2


def encode_merged_block(cells: list[tuple[int, list[bytes]]],
                        center_lon_micro: int,
                        center_lat_micro: int) -> bytes:
    """Wrap a sorted list of (h3_cell, [record_bytes]) into a v2 merged block.

    Layout (post-decompression):
        i32 center_lon_micro
        i32 center_lat_micro
        u32 cell_count
        per cell (sorted): u64 cell_id, u32 record_offset
        u8[] record_data   (each record is u32 record_len + record_body)

    record_offset values are relative to the start of record_data (the byte
    span after the cells table). Records within record_data use the v1
    per-record format so existing decoders work unchanged.

    A solo block is just cells=[(cell_id, records)] with cell_count==1.
    """
    sorted_cells = sorted(cells, key=lambda c: c[0])

    record_data = bytearray()
    offsets: list[int] = []
    for _, records in sorted_cells:
        offsets.append(len(record_data))
        for rec in records:
            record_data.extend(rec)

    buf = bytearray()
    buf.extend(struct.pack("<iiI", center_lon_micro, center_lat_micro,
                           len(sorted_cells)))
    for (cell_id, _), off in zip(sorted_cells, offsets):
        buf.extend(struct.pack("<QI", cell_id, off))
    buf.extend(record_data)
    return bytes(buf)


def decode_merged_block(data: bytes) -> dict:
    """Decode a v2 merged block. Returns dict with center, cells, records.

    Returns:
        {
          "center_lon_micro": int, "center_lat_micro": int,
          "cell_count": int,
          "cells": [(cell_id, [record_body_bytes]), ...],
        }
    """
    center_lon_micro, center_lat_micro, cell_count = struct.unpack_from(
        "<iiI", data, 0)
    pos = 12
    cell_entries = []
    for _ in range(cell_count):
        cell_id, off = struct.unpack_from("<QI", data, pos)
        cell_entries.append((cell_id, off))
        pos += 12

    record_data_start = pos
    record_data_end = len(data)

    cells = []
    for i, (cell_id, off) in enumerate(cell_entries):
        abs_start = record_data_start + off
        abs_end = (record_data_start + cell_entries[i + 1][1]
                   if i + 1 < cell_count else record_data_end)
        records: list[bytes] = []
        p = abs_start
        while p < abs_end:
            rec_len = struct.unpack_from("<I", data, p)[0]
            p += 4
            records.append(bytes(data[p:p + rec_len]))
            p += rec_len
        cells.append((cell_id, records))

    return {
        "center_lon_micro": center_lon_micro,
        "center_lat_micro": center_lat_micro,
        "cell_count": cell_count,
        "cells": cells,
    }


def encode_coords_u16(coords: list[tuple[float, float]],
                      center_lon_micro: int,
                      center_lat_micro: int) -> bytes:
    """Encode coordinates as u16 absolute (first) + zigzag-varint deltas.

    First vertex: u16 lon_bias, u16 lat_bias  (bias 32768 around center).
    Subsequent: zigzag varint deltas in the biased-u16 space (which equal
    deltas in micro space since bias cancels).
    """
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


def decode_coords_u16(data: bytes, pos: int, center_lon_micro: int,
                      center_lat_micro: int,
                      vertex_count: int) -> tuple[list[tuple[float, float]], int]:
    """Decode u16-encoded coordinate sequence. Returns (coords, bytes_consumed)."""
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


def encode_index_entry_v2(h3_cell: int,
                          min_lon_micro: int, min_lat_micro: int,
                          max_lon_micro: int, max_lat_micro: int,
                          block_offset: int, block_length: int,
                          feature_count: int, cell_index: int) -> bytes:
    """Encode v2 spatial index entry (37 bytes)."""
    buf = struct.pack("<Qiiii", h3_cell,
                      min_lon_micro, min_lat_micro,
                      max_lon_micro, max_lat_micro)
    buf += block_offset.to_bytes(6, "little")
    buf += block_length.to_bytes(3, "little")
    buf += struct.pack("<HH",
                       min(feature_count, 65535),
                       min(cell_index, 65535))
    return buf


def decode_index_entry_v2(data: bytes, pos: int) -> dict:
    """Decode a 37-byte v2 spatial index entry."""
    h3_cell, min_lon, min_lat, max_lon, max_lat = struct.unpack_from(
        "<Qiiii", data, pos)
    block_offset = int.from_bytes(data[pos + 24:pos + 30], "little")
    block_length = int.from_bytes(data[pos + 30:pos + 33], "little")
    feature_count, cell_index = struct.unpack_from("<HH", data, pos + 33)
    return {
        "h3_cell": h3_cell,
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
        "block_offset": block_offset,
        "block_length": block_length,
        "feature_count": feature_count,
        "cell_index": cell_index,
    }


def build_v2_file(layer_data: list, output_path: str, magic: bytes,
                  version: int, compression_level: int = 9,
                  merge_threshold: int = 100) -> list[dict]:
    """Build a complete v2 ptiles file.

    layer_data: list of feature dicts. Each feature must have keys:
        h3_cell: int       -- H3 res-7 cell containing the feature
        coords: list[(lon, lat)]  -- raw geometry in degrees
        attrs:  bytes      -- pre-encoded attribute payload (everything in
                              the record body that follows the coordinates)

    The full v2 record body is built as:
        vertex_count (u16) + u16-encoded coords + attrs

    Returns the list of v2 index entry dicts written to the file.
    """
    import h3

    # 1. Group features by H3 cell
    cells: dict[int, list[dict]] = {}
    for feat in layer_data:
        cells.setdefault(feat["h3_cell"], []).append(feat)

    sorted_cell_ids = sorted(cells)

    # Helper: per-cell bbox (microdegrees) from all features' coords
    def cell_bbox_micro(feats: list[dict]) -> tuple[int, int, int, int]:
        min_lon = min_lat = 10**9
        max_lon = max_lat = -(10**9)
        for f in feats:
            for lon, lat in f["coords"]:
                lon_m = round(lon * 100_000)
                lat_m = round(lat * 100_000)
                if lon_m < min_lon:
                    min_lon = lon_m
                if lon_m > max_lon:
                    max_lon = lon_m
                if lat_m < min_lat:
                    min_lat = lat_m
                if lat_m > max_lat:
                    max_lat = lat_m
        return min_lon, min_lat, max_lon, max_lat

    # Helper: rough size estimate (vertices * 4 + attrs)
    def estimate_size(feats: list[dict]) -> int:
        s = 0
        for f in feats:
            s += 2 + 4 + max(0, len(f["coords"]) - 1) * 4 + len(f["attrs"])
            s += 4  # u32 record_len prefix
        return s

    # 2 + 3 + 4. Walk sorted cells, group sparse cells into merged blocks.
    # u16 cell-relative coords have a +/-32768 microdegree range from the
    # group centroid. H3-ID sort order is mostly-but-not-perfectly spatial,
    # so we also close a merge run when the spatial span would overflow u16.
    BlockGroup = list[tuple[int, list[dict]]]  # [(cell_id, [features])]
    block_groups: list[BlockGroup] = []
    pending: BlockGroup = []
    pending_size = 0
    pending_min_lat = pending_max_lat = 0.0
    pending_min_lon = pending_max_lon = 0.0

    # Safety margin: cell-relative coords can sit up to ~1.5km from cell
    # center, so keep the run span well below 65536 microdegrees.
    U16_SPAN_LIMIT = 60_000  # microdegrees

    cell_center_cache: dict[int, tuple[float, float]] = {}

    def cell_center(cid: int) -> tuple[float, float]:
        c = cell_center_cache.get(cid)
        if c is None:
            c = h3.cell_to_latlng(h3.int_to_str(cid))
            cell_center_cache[cid] = c
        return c

    for cid in sorted_cell_ids:
        feats = cells[cid]
        if len(feats) >= merge_threshold:
            if pending:
                block_groups.append(pending)
                pending, pending_size = [], 0
            block_groups.append([(cid, feats)])
            continue

        est = estimate_size(feats)
        clat, clon = cell_center(cid)

        if pending:
            new_min_lat = min(pending_min_lat, clat)
            new_max_lat = max(pending_max_lat, clat)
            new_min_lon = min(pending_min_lon, clon)
            new_max_lon = max(pending_max_lon, clon)
            span_lat = (new_max_lat - new_min_lat) * 100_000
            span_lon = (new_max_lon - new_min_lon) * 100_000
            overflow = span_lat > U16_SPAN_LIMIT or span_lon > U16_SPAN_LIMIT
            if pending_size + est > 16 * 1024 or overflow:
                block_groups.append(pending)
                pending, pending_size = [], 0

        if not pending:
            pending_min_lat = pending_max_lat = clat
            pending_min_lon = pending_max_lon = clon
        else:
            if clat < pending_min_lat:
                pending_min_lat = clat
            if clat > pending_max_lat:
                pending_max_lat = clat
            if clon < pending_min_lon:
                pending_min_lon = clon
            if clon > pending_max_lon:
                pending_max_lon = clon

        pending.append((cid, feats))
        pending_size += est
    if pending:
        block_groups.append(pending)

    # 5 + 6. Encode each block: centroid -> u16 coords -> merged block bytes
    raw_blocks: list[bytes] = []
    per_block_index_data: list[list[dict]] = []  # one list per block

    for group in block_groups:
        # Centroid: midpoint of the cell-center bbox (cell_to_latlng returns
        # lat, lon). Bbox-midpoint keeps worst-case offset bounded by half
        # the span, which is what u16 cell-relative encoding needs.
        lats_lons = [h3.cell_to_latlng(h3.int_to_str(cid)) for cid, _ in group]
        lats = [p[0] for p in lats_lons]
        lons = [p[1] for p in lats_lons]
        center_lat = (min(lats) + max(lats)) / 2
        center_lon = (min(lons) + max(lons)) / 2
        center_lon_micro = round(center_lon * 100_000)
        center_lat_micro = round(center_lat * 100_000)

        cells_for_block: list[tuple[int, list[bytes]]] = []
        per_cell_index: list[dict] = []

        for idx, (cid, feats) in enumerate(group):
            record_bytes_list: list[bytes] = []
            for f in feats:
                coords = f["coords"]
                body = struct.pack("<H", len(coords))
                body += encode_coords_u16(coords, center_lon_micro,
                                          center_lat_micro)
                body += f["attrs"]
                record_bytes_list.append(struct.pack("<I", len(body)) + body)
            cells_for_block.append((cid, record_bytes_list))

            mn_lon, mn_lat, mx_lon, mx_lat = cell_bbox_micro(feats)
            per_cell_index.append({
                "h3_cell": cid,
                "min_lon": mn_lon, "min_lat": mn_lat,
                "max_lon": mx_lon, "max_lat": mx_lat,
                "feature_count": len(feats),
                "cell_index": idx,
            })

        raw = encode_merged_block(cells_for_block,
                                  center_lon_micro, center_lat_micro)
        raw_blocks.append(raw)
        per_block_index_data.append(per_cell_index)

    # 7. Train dictionary from a sample of raw blocks
    dict_samples = raw_blocks if len(raw_blocks) >= 7 else raw_blocks * (
        (7 // max(len(raw_blocks), 1)) + 1)
    dict_samples = [s for s in dict_samples if s]
    if dict_samples:
        try:
            dict_bytes = train_dictionary(dict_samples, dict_size=min(
                512 * 1024, sum(len(s) for s in dict_samples) // 2 + 1))
        except Exception:
            dict_bytes = b""
    else:
        dict_bytes = b""

    # Compress blocks (with dict if present)
    if dict_bytes:
        compressed_blocks = [compress_block(b, dict_bytes, level=compression_level)
                             for b in raw_blocks]
    else:
        cctx = zstd.ZstdCompressor(level=compression_level)
        compressed_blocks = [cctx.compress(b) for b in raw_blocks]

    # Compute global bbox + feature count
    if layer_data:
        all_lons = [lon for f in layer_data for lon, _ in f["coords"]]
        all_lats = [lat for f in layer_data for _, lat in f["coords"]]
        g_min_lon, g_max_lon = min(all_lons), max(all_lons)
        g_min_lat, g_max_lat = min(all_lats), max(all_lats)
    else:
        g_min_lon = g_max_lon = g_min_lat = g_max_lat = 0.0
    total_feature_count = len(layer_data)

    # 8. Write file: header (placeholder) -> dictionary -> index -> blocks
    HEADER_LEN = HEADER_SIZE
    dict_offset = HEADER_LEN
    dict_length = len(dict_bytes)

    index_offset = dict_offset + dict_length
    # Index = u32 entry_count + sum(per-cell entries)
    total_entries = sum(len(p) for p in per_block_index_data)
    index_length = 4 + total_entries * INDEX_ENTRY_SIZE_V2
    blocks_offset = index_offset + index_length

    # Assign block offsets and assemble final index entries
    cur_block_off = blocks_offset
    index_entries: list[dict] = []
    for cblock, per_cell in zip(compressed_blocks, per_block_index_data):
        block_len = len(cblock)
        for pc in per_cell:
            index_entries.append({
                **pc,
                "block_offset": cur_block_off,
                "block_length": block_len,
            })
        cur_block_off += block_len

    # Sort index by h3_cell (should already be in order)
    index_entries.sort(key=lambda e: e["h3_cell"])

    with open(output_path, "wb") as f:
        write_header(f, magic=magic, version=version,
                     min_lat=g_min_lat, min_lon=g_min_lon,
                     max_lat=g_max_lat, max_lon=g_max_lon,
                     feature_count=total_feature_count,
                     block_count=len(compressed_blocks),
                     dict_offset=dict_offset, dict_length=dict_length,
                     index_offset=index_offset, index_length=index_length,
                     blocks_offset=blocks_offset)
        if dict_bytes:
            f.write(dict_bytes)
        f.write(struct.pack("<I", len(index_entries)))
        for e in index_entries:
            f.write(encode_index_entry_v2(
                e["h3_cell"],
                e["min_lon"], e["min_lat"], e["max_lon"], e["max_lat"],
                e["block_offset"], e["block_length"],
                e["feature_count"], e["cell_index"]))
        for cblock in compressed_blocks:
            f.write(cblock)

    return index_entries
