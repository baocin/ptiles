"""
Shared encoding/decoding primitives for PTiles format.

Refactored from scripts/shared.py. Provides varint, zigzag,
coordinate encoding/decoding, PTiles header parsing, spatial index
parsing, zstd dictionary-based compression, and string encoding helpers.
"""

import io
import struct
import zstandard as zstd

__all__ = [
    "encode_varint", "decode_varint",
    "zigzag_encode", "zigzag_decode",
    "coord_to_micro", "micro_to_coord",
    "encode_coordinates", "decode_coordinates",
    "encode_string_u16", "encode_string_u8",
    "decode_string_u16", "decode_string_u8",
    "encode_indexed_or_custom", "decode_indexed_or_custom",
    "HEADER_SIZE", "HEADER_STRUCT",
    "write_header", "read_header",
    "INDEX_ENTRY_SIZE",
    "encode_index_entry", "decode_index_entry",
    "write_index", "read_index", "binary_search_index",
    "train_dictionary", "compress_block", "decompress_block",
    "build_string_table", "encode_string_table", "decode_string_table",
    "encode_table_ref", "decode_table_ref",
    "BTYPE_INDEX", "BTYPE_REVERSE", "USE_MAP", "USE_REVERSE",
    "ROAD_CLASS_REVERSE", "SURFACE_REVERSE",
    "WATER_TYPES",
    "decode_water_record",
    # v2 additions
    "INDEX_ENTRY_SIZE_V2",
    "decode_coords_u16",
    "decode_index_entry_v2", "decode_index_v2",
    "decode_merged_block_header",
]


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

# Fields:
# magic(7) + null(1) + version(1) + pad(3) + min_lat(4) + min_lon(4)
# + max_lat(4) + max_lon(4) + feature_count(8) + block_count(4)
# + dict_offset(8) + dict_length(4) + index_offset(8) + index_length(4)
# + blocks_offset(8) + aux_offset(8) + aux_length(4) + reserved(172)


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
    """Decode indexed byte or 255 + custom string."""
    idx = data[pos]
    if idx == 255:
        s, consumed = decode_string_u8(data, pos + 1)
        return s, 1 + consumed
    return reverse_index.get(idx, "unknown"), 1


# --- Per-Cell String Table (v8) ---

def build_string_table(strings: list[str]) -> tuple[list[str], dict[str, int]]:
    """Build a deduplicated string table for a block."""
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


# --- Building Type Index ---

BTYPE_INDEX = [
    "yes", "house", "residential", "commercial", "industrial",
    "retail", "garage", "apartments", "office", "warehouse",
    "shed", "detached", "terrace", "school", "church",
    "hospital", "hotel", "roof", "construction", "barn",
]

BTYPE_REVERSE = {i: t for i, t in enumerate(BTYPE_INDEX)}

USE_MAP = {
    "house": 1, "residential": 1, "detached": 1, "terrace": 1, "apartments": 1,
    "commercial": 2, "retail": 2, "office": 2, "warehouse": 2, "hotel": 2,
    "industrial": 3, "school": 3, "church": 3, "hospital": 3, "public": 3,
}

USE_REVERSE = {1: "residential", 2: "commercial", 3: "industrial/institutional"}

# --- Road Class / Surface Index ---

ROAD_CLASS_REVERSE = {
    0: "motorway", 1: "motorway_link", 2: "trunk", 3: "trunk_link",
    4: "primary", 5: "primary_link", 6: "secondary", 7: "tertiary",
    8: "residential", 9: "service", 10: "track", 11: "footway",
    12: "cycleway", 13: "path", 14: "pedestrian", 15: "tertiary_link",
}

SURFACE_REVERSE = {
    0: "paved", 1: "asphalt", 2: "concrete", 3: "unpaved",
    4: "gravel", 5: "dirt", 6: "sand", 7: "grass",
}

# --- Water Types ---

WATER_TYPES = [
    "lake", "reservoir", "pond", "river", "stream",
    "creek", "canal", "drain", "bay", "ocean",
    "wetland", "marsh", "swamp", "estuary",
]


# ===========================================================================
# v2 Format Support (SPEC_v2.md)
# ===========================================================================
#
# v2 reader additions. v1 paths above remain untouched.
#   - 37-byte index entries with per-cell bbox + cell_index_in_block
#   - Merged blocks (multiple sparse H3 cells share one zstd block)
#   - u16 cell-relative coordinates (saves bytes vs i32)

INDEX_ENTRY_SIZE_V2 = 37  # 8 + 4*4 + 6 + 3 + 2 + 2


def decode_coords_u16(data: bytes, pos: int, center_lon_micro: int,
                      center_lat_micro: int,
                      vertex_count: int) -> tuple[list[tuple[float, float]], int]:
    """Decode u16-encoded coordinate sequence. Returns (coords, bytes_consumed).

    Layout:
      first vertex: u16 ux, u16 uy (lon/lat biased by 32768 around center)
      subsequent:   zigzag varint deltas in microdegrees
    """
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


def decode_index_v2(data: bytes) -> list[dict]:
    """Parse a v2 spatial index from bytes (u32 entry_count + entries)."""
    entry_count = struct.unpack_from("<I", data, 0)[0]
    entries = []
    pos = 4
    for _ in range(entry_count):
        entries.append(decode_index_entry_v2(data, pos))
        pos += INDEX_ENTRY_SIZE_V2
    return entries


def decode_merged_block_header(data: bytes) -> dict:
    """Parse the header of a v2 merged block.

    Layout:
      i32 center_lon_micro
      i32 center_lat_micro
      u32 cell_count
      per-cell: u64 cell_id, u32 record_offset
      (record_data follows; offsets are relative to its start)

    Returns dict with:
      center_lon_micro, center_lat_micro, cell_count,
      cell_offsets: list[(cell_id, record_offset)],
      record_data_offset: byte position where record_data starts in `data`.
    """
    center_lon_micro, center_lat_micro, cell_count = struct.unpack_from(
        "<iiI", data, 0)
    pos = 12
    cell_offsets: list[tuple[int, int]] = []
    for _ in range(cell_count):
        cell_id, record_offset = struct.unpack_from("<QI", data, pos)
        cell_offsets.append((cell_id, record_offset))
        pos += 12
    return {
        "center_lon_micro": center_lon_micro,
        "center_lat_micro": center_lat_micro,
        "cell_count": cell_count,
        "cell_offsets": cell_offsets,
        "record_data_offset": pos,
    }
