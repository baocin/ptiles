#!/usr/bin/env python3
"""
PTILES v8 Building Record Encoder

Packing improvements over v6:
- Cell-relative first vertex (i16 offsets vs int32, saves 4 bytes/record)
- Per-cell string table (1-byte name/type refs vs full strings)
- Vertex count bias (4-19 verts packed into 4 bits)
- Height tier + building use classification (2+2 bits)

Record format (v8):
  osm_id     varint(delta)        Delta from previous OSM ID
  flags      u8                   Bit flags for optional fields
    0-1: use_class             0=unknown, 1=residential, 2=commercial, 3=industrial/inst
    2-3: height_tier           0=unknown, 1=1-2fl, 2=3-5fl, 3=6+fl
    4-7: vertex_count_packed   0=raw follows, 1-15=vcount-4
  vertex_raw u8 (only if packed==0)
  first_lon  i16 (cell-relative)
  first_lat  i16 (cell-relative)
  deltas     varint pairs        Zigzag delta lon/lat per vertex
  btype      u8                  Building type table index
  btype_str  u8_len+UTF-8 (only if btype==0xff)
  flags2     u8                  Extended flags
    0x01: has_name
    0x02: has_category
    0x04: has_name_source
    0x08: has_poi_osm_id
    0x10: has_height_m (raw u8 half-meters)
  name       u8 table_ref (only if has_name)
  category   u8 table_ref (only if has_category)
  name_src   u8 table_ref (only if has_name_source)
  poi_osm_id u64 (only if has_poi_osm_id)
  height_raw u8 (only if has_height_m, 0.5m steps)

The first 4 bits of 'flags' contain use_class. The remaining 4 bits
contain vertex_count when packed, or 0 + raw byte follows.

Block format:
  string_table  variable (uleb + entries)
  records       variable (uncompressed building data)
"""

import struct
import h3
from shared import (
    encode_varint, decode_varint,
    zigzag_encode, zigzag_decode,
    encode_coordinates, decode_coordinates,
    coord_to_micro, micro_to_coord,
    encode_string_u8, decode_string_u8,
    build_string_table,
    encode_string_table, encode_table_ref,
    decode_string_table, decode_table_ref,
    BTYPE_INDEX, BTYPE_REVERSE,
    USE_MAP, USE_REVERSE,
)


HEIGHT_TIERS = {0: "unknown", 1: "1-2", 2: "3-5", 3: "6+"}


def classify_height_tier(height_m: float | None) -> int:
    """Classify height in meters to tier."""
    if height_m is None or height_m <= 0:
        return 0
    if height_m <= 6:
        return 1
    if height_m <= 15:
        return 2
    return 3


def classify_use(btype: str) -> int:
    """Classify building type to use class."""
    return USE_MAP.get(btype, 0)


def encode_building_v8(building: dict, prev_osm_id: int,
                       cell_center: tuple[float, float],
                       string_lookup: dict[str, int],
                       prev_name: str = "",
                       prev_btype: str = "") -> tuple[bytes, int, str, str]:
    """Encode a single building record in v8 format.

    Args:
        building: Dict with osm_id, coords, building_type, name, etc.
        prev_osm_id: OSM ID of previous building (for delta)
        cell_center: (lon, lat) of H3 cell center
        string_lookup: Name -> index mapping for string table refs
        prev_name: Previous building name (for potential future RLE)
        prev_btype: Previous building type (for potential future type runs)

    Returns:
        (record_bytes, osm_id, name, btype)
    """
    buf = bytearray()
    osm_id = building["osm_id"]
    coords = building.get("coords", [])
    vertex_count = len(coords)
    btype = building.get("building_type", "yes")
    name = building.get("name", "")
    category = building.get("category", "")
    name_source = building.get("name_source", "")
    poi_osm_id = building.get("poi_osm_id", 0)
    height_m = building.get("height_m")

    # 1. OSM ID delta
    delta = osm_id - prev_osm_id
    buf.extend(encode_varint(zigzag_encode(delta)))

    # 2. Flags byte: use_class (bits 0-1) + height_tier (bits 2-3) + vertex_count (bits 4-7)
    use = classify_use(btype)
    height_tier = classify_height_tier(height_m)
    flags = (use & 0x03) | ((height_tier & 0x03) << 2)
    # Vertex count in flags or raw
    if 4 <= vertex_count <= 18:
        flags |= ((vertex_count - 4) << 4)
        vc_raw = False
    else:
        flags |= (0x0F << 4)  # sentinel: raw u8 follows
        vc_raw = True

    buf.append(flags)

    if vc_raw:
        if vertex_count > 255:
            # Simplify polygon: take every Nth vertex to fit in u8
            step = (vertex_count + 254) // 255
            coords = coords[::step]
            vertex_count = len(coords)
        buf.append(vertex_count)

    # 3. Cell-relative first vertex (i16 offsets) + delta-encoded remaining vertices
    if vertex_count > 0:
        offset_lon = coord_to_micro(coords[0][0]) - coord_to_micro(cell_center[0])
        offset_lat = coord_to_micro(coords[0][1]) - coord_to_micro(cell_center[1])
        buf.extend(struct.pack("<hh", offset_lon, offset_lat))

        if vertex_count > 1:
            # Delta-encode vertices 1..N from vertex 0
            prev_lon = coord_to_micro(coords[0][0])
            prev_lat = coord_to_micro(coords[0][1])
            for lon, lat in coords[1:]:
                cur_lon = coord_to_micro(lon)
                cur_lat = coord_to_micro(lat)
                buf.extend(encode_varint(zigzag_encode(cur_lon - prev_lon)))
                buf.extend(encode_varint(zigzag_encode(cur_lat - prev_lat)))
                prev_lon, prev_lat = cur_lon, cur_lat

    # 5. Building type (table reference or inline)
    buf.extend(encode_table_ref(btype, string_lookup))

    # 6. Extended flags (flags2)
    flags2 = 0
    if name:
        flags2 |= 0x01
    if category:
        flags2 |= 0x02
    if name_source:
        flags2 |= 0x04
    if poi_osm_id:
        flags2 |= 0x08
    if height_m is not None and height_m > 0:
        flags2 |= 0x10

    buf.append(flags2)

    # 7. Optional fields
    if name:
        buf.extend(encode_table_ref(name, string_lookup))
    if category:
        buf.extend(encode_table_ref(category, string_lookup))
    if name_source:
        buf.extend(encode_table_ref(name_source, string_lookup))
    if poi_osm_id:
        buf.extend(struct.pack("<Q", poi_osm_id))
    if height_m is not None and height_m > 0:
        h = min(255, round(height_m * 2))  # 0.5m steps, clamp to 127.5m
        buf.append(h)

    return bytes(buf), osm_id, name, btype


def encode_block_v8(buildings: list[dict], cell: int,
                    cell_centers: dict[int, tuple[float, float]] | None = None
                    ) -> tuple[bytes, int]:
    """Encode a full v8 block with string table.

    Args:
        buildings: Sorted list of building dicts (by OSM ID)
        cell: H3 cell integer
        cell_centers: Cache of cell -> (lon, lat) centers

    Returns:
        (block_bytes, feature_count)
    """
    # Build string table from all strings in block
    all_strings = []
    for b in buildings:
        all_strings.append(b.get("building_type", "yes"))
        if b.get("name"):
            all_strings.append(b["name"])
        if b.get("category"):
            all_strings.append(b["category"])
        if b.get("name_source"):
            all_strings.append(b["name_source"])

    table, lookup = build_string_table(all_strings)

    # Get cell center
    if cell_centers and cell in cell_centers:
        center = cell_centers[cell]
    else:
        if isinstance(cell, int):
            cell_hex = hex(cell)[2:]
        else:
            cell_hex = str(cell)
        lat, lon = h3.cell_to_latlng(cell_hex)
        center = (lon, lat)

    # Encode string table
    block_buf = bytearray()
    block_buf.extend(encode_string_table(table))

    # Encode records
    prev_osm_id = 0
    prev_name = ""
    prev_btype = ""
    for b in buildings:
        record, prev_osm_id, prev_name, prev_btype = encode_building_v8(
            b, prev_osm_id, center, lookup, prev_name, prev_btype
        )
        # Record length prefix
        block_buf.extend(struct.pack("<I", len(record)))
        block_buf.extend(record)

    return bytes(block_buf), len(buildings)


def decode_building_v8(data: bytes, offset: int, prev_osm_id: int,
                       cell_center: tuple[float, float],
                       string_table: list[str]) -> dict:
    """Decode a single v8 building record.

    Args:
        data: Raw block data
        offset: Byte offset to start of record
        prev_osm_id: OSM ID of previous building in block
        cell_center: (lon, lat) of H3 cell center
        string_table: Decoded string table for this block

    Returns:
        Decoded building dict
    """
    pos = offset

    # 1. OSM ID delta
    osm_id_delta, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(osm_id_delta)

    # 2. Flags
    flags = data[pos]
    pos += 1

    use_class = flags & 0x03
    height_tier = (flags >> 2) & 0x03
    vc_packed = (flags >> 4) & 0x0F

    # Vertex count (0x0F sentinel = raw u8 follows)
    if vc_packed == 0x0F:
        vertex_count = data[pos]
        pos += 1
    else:
        vertex_count = vc_packed + 4

    # 3. Cell-relative first vertex + delta-encoded remaining vertices
    coords = []
    if vertex_count > 0:
        offset_lon, offset_lat = struct.unpack_from("<hh", data, pos)
        pos += 4
        prev_lon = coord_to_micro(cell_center[0]) + offset_lon
        prev_lat = coord_to_micro(cell_center[1]) + offset_lat
        coords.append([prev_lon / 100_000, prev_lat / 100_000])

        for _ in range(vertex_count - 1):
            dlon_raw, consumed = decode_varint(data, pos)
            pos += consumed
            dlat_raw, consumed = decode_varint(data, pos)
            pos += consumed
            prev_lon += zigzag_decode(dlon_raw)
            prev_lat += zigzag_decode(dlat_raw)
            coords.append([prev_lon / 100_000, prev_lat / 100_000])

    # 4. Building type
    btype_idx = data[pos]
    pos += 1
    if btype_idx == 0xff:
        btype, consumed = decode_string_u8(data, pos)
        pos += consumed
    elif btype_idx < len(string_table):
        btype = string_table[btype_idx]
    else:
        btype = "yes"

    # 5. Extended flags
    flags2 = data[pos]
    pos += 1
    has_name = flags2 & 0x01
    has_category = flags2 & 0x02
    has_name_source = flags2 & 0x04
    has_poi_osm_id = flags2 & 0x08
    has_height_m = flags2 & 0x10

    building = {
        "osm_id": osm_id,
        "building_type": btype,
        "use": USE_REVERSE.get(use_class, "unknown"),
        "height_tier": HEIGHT_TIERS.get(height_tier, "unknown"),
        "geometry": {"type": "Polygon", "coordinates": [coords]} if coords else None,
    }

    # Optional fields
    if has_name:
        name, consumed = decode_table_ref(data, pos, string_table)
        building["name"] = name
        pos += consumed
    if has_category:
        cat, consumed = decode_table_ref(data, pos, string_table)
        building["category"] = cat
        pos += consumed
    if has_name_source:
        src, consumed = decode_table_ref(data, pos, string_table)
        building["name_source"] = src
        pos += consumed
    if has_poi_osm_id:
        building["poi_osm_id"] = struct.unpack_from("<Q", data, pos)[0]
        pos += 8
    if has_height_m:
        building["height_m"] = data[pos] * 0.5
        pos += 1

    # Calculate centroid
    if coords:
        lats = [c[1] for c in coords]
        lons = [c[0] for c in coords]
        building["centroid_lat"] = round(sum(lats) / len(lats), 6)
        building["centroid_lon"] = round(sum(lons) / len(lons), 6)

    return building


if __name__ == "__main__":
    # Quick smoke test
    test_bldg = {
        "osm_id": 12345678,
        "coords": [[-86.7816, 36.1627], [-86.7816, 36.1630],
                   [-86.7813, 36.1630], [-86.7813, 36.1627]],
        "building_type": "house",
        "name": "Test House",
        "height_m": 6.0,
    }
    cell = h3.latlng_to_cell(36.16285, -86.78145, 7)
    if isinstance(cell, str):
        cell = int(cell, 16)
    if isinstance(cell, int):
        cell_hex = hex(cell)[2:]
    else:
        cell_hex = str(cell)
    lat, lon = h3.cell_to_latlng(cell_hex)
    center = (lon, lat)

    block, count = encode_block_v8([test_bldg], cell, {cell: center})
    print(f"Encoded {count} building(s) in {len(block)} bytes")
    print(f"Block hex (first 80): {block[:80].hex()}")

    # Decode
    table, pos = decode_string_table(block, 0)
    print(f"String table: {table} (decoded at pos {pos})")

    record_len = struct.unpack_from("<I", block, pos)[0]
    pos += 4
    decoded = decode_building_v8(block, pos, 0, center, table)
    print(f"Decoded: {decoded}")
