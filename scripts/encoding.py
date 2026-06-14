"""
Pure serialization primitives for PTiles format.

No file paths, no zstd imports, no dataset-specific constants.
Only functions that transform bytes to bytes or values to bytes.
"""

import struct


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
    return (n << 1) ^ (n >> 63)


def zigzag_decode(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def coord_to_micro(deg: float) -> int:
    return round(deg * 100_000)


def micro_to_coord(micro: int) -> float:
    return micro / 100_000


def encode_coordinates(coords: list[tuple[float, float]]) -> tuple[bytes, int, int]:
    """Encode coordinate sequence as first absolute + zigzag varint deltas.

    Returns (delta_bytes, first_lon_micro, first_lat_micro)."""
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


def decode_coordinates(
    data: bytes, pos: int, first_lon: int, first_lat: int, vertex_count: int
) -> tuple[list[tuple[float, float]], int]:
    """Decode delta-encoded coordinate sequence.

    Returns (coords_list, bytes_consumed)."""
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


def encode_string_u16(s: str) -> bytes:
    encoded = s.encode("utf-8")
    return struct.pack("<H", len(encoded)) + encoded


def encode_string_u8(s: str) -> bytes:
    encoded = s.encode("utf-8")
    if len(encoded) > 255:
        encoded = encoded[:255]
    return struct.pack("B", len(encoded)) + encoded


def decode_string_u16(data: bytes, pos: int) -> tuple[str, int]:
    slen = struct.unpack_from("<H", data, pos)[0]
    s = data[pos + 2 : pos + 2 + slen].decode("utf-8")
    return s, 2 + slen


def decode_string_u8(data: bytes, pos: int) -> tuple[str, int]:
    slen = data[pos]
    s = data[pos + 1 : pos + 1 + slen].decode("utf-8")
    return s, 1 + slen


def encode_indexed_or_custom(value: str, index: dict[str, int]) -> bytes:
    if value in index:
        return struct.pack("B", index[value])
    return struct.pack("B", 255) + encode_string_u8(value)


def decode_indexed_or_custom(
    data: bytes, pos: int, reverse_index: dict[int, str]
) -> tuple[str, int]:
    idx = data[pos]
    if idx == 255:
        s, consumed = decode_string_u8(data, pos + 1)
        return s, 1 + consumed
    return reverse_index.get(idx, "unknown"), 1


def build_string_table(strings: list[str]) -> tuple[list[str], dict[str, int]]:
    seen = {}
    table = []
    for s in strings:
        if s not in seen:
            seen[s] = len(table)
            table.append(s)
    return table, seen


def encode_string_table(table: list[str]) -> bytes:
    buf = bytearray()
    buf.append(min(len(table), 255))
    for s in table[:255]:
        buf.extend(encode_string_u8(s))
    return bytes(buf)


def decode_string_table(data: bytes, pos: int) -> tuple[list[str], int]:
    count = data[pos]
    pos += 1
    table = []
    for _ in range(count):
        s, consumed = decode_string_u8(data, pos)
        table.append(s)
        pos += consumed
    return table, pos


def encode_table_ref(value: str, table_lookup: dict[str, int]) -> bytes:
    if value in table_lookup:
        idx = table_lookup[value]
        if idx <= 254:
            return struct.pack("B", idx)
    return struct.pack("B", 0xFF) + encode_string_u8(value)


def decode_table_ref(data: bytes, pos: int, table: list[str]) -> tuple[str, int]:
    idx = data[pos]
    pos += 1
    if idx == 0xFF:
        s, consumed = decode_string_u8(data, pos)
        return s, 1 + consumed
    if idx < len(table):
        return table[idx], 1
    return "", 1


def encode_cell_relative(
    lon: float, lat: float, cell_center_lon: float, cell_center_lat: float
) -> bytes:
    offset_lon = coord_to_micro(lon) - coord_to_micro(cell_center_lon)
    offset_lat = coord_to_micro(lat) - coord_to_micro(cell_center_lat)
    return struct.pack("<hh", offset_lon, offset_lat)


def decode_cell_relative(
    data: bytes, pos: int, cell_center_lon: float, cell_center_lat: float
) -> tuple[float, float]:
    offset_lon, offset_lat = struct.unpack_from("<hh", data, pos)
    lon = (coord_to_micro(cell_center_lon) + offset_lon) / 100_000
    lat = (coord_to_micro(cell_center_lat) + offset_lat) / 100_000
    return lon, lat


def encode_vertex_count(vc: int, extra_flags: int = 0) -> tuple[int, bytes | None]:
    if 4 <= vc <= 18:
        return (extra_flags | ((vc - 4) << 4)), None
    return (extra_flags | (0x0F << 4)), struct.pack("B", vc)


def decode_vertex_count(
    flags_byte: int, data: bytes | None = None, pos: int = 0
) -> tuple[int, int]:
    packed = (flags_byte >> 4) & 0x0F
    if packed == 0x0F:
        return data[pos], 1
    return packed + 4, 0
