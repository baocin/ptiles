#!/usr/bin/env python3
"""
Build a business name index PTILES file for a given state.

Read an existing {STATE}.business.ptiles file, iterate all blocks,
extract business names, group by first letter, write a new PTILES file
with letter-prefix keys.

Usage:
    uv run python build_business_name_index.py TN
"""

import sys
import os
import glob
import struct
from collections import defaultdict

# Ensure local imports work
sys.path.insert(0, os.path.dirname(__file__))

from shared import (
    HEADER_SIZE,
    INDEX_ENTRY_SIZE,
    read_header,
    read_index,
    decode_varint,
    decode_string_u16,
    decode_string_u8,
    decompress_block,
    write_header,
)
from encoding import encode_string_u16, encode_string_u8

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "states")

MAGIC = b"PTILESX\0"
VERSION = 1


def _fold_name(s: str) -> str:
    """Accent-fold + lowercase, matching the Rust client's
    `business_search::fold_name`: NFD-decompose, drop combining marks
    (U+0300..U+036F), lowercase, then ß→ss. Keeps the builder's bucketing in
    agreement with the client so accented-first-letter names (e.g. "Éclair")
    bucket to their base letter ('e') rather than the catch-all 26.
    """
    import unicodedata

    decomposed = unicodedata.normalize("NFD", s)
    stripped = "".join(c for c in decomposed if not (0x0300 <= ord(c) <= 0x036F))
    return stripped.lower().replace("ß", "ss")


def name_to_key(name: str) -> int:
    """Map the first character of a business name to a letter key (0-27).

    Accent-folded first: a-z → 0-25, digit/other non-letter start → 26,
    empty/no name → 27. See `_fold_name` — this matches the Rust client's
    `name_to_key`, so a rebuilt sidecar buckets accented names correctly.
    """
    if not name:
        return 27
    first = _fold_name(name.strip())
    if not first:
        return 27
    c = first[0]
    if "a" <= c <= "z":
        return ord(c) - ord("a")
    return 26


def decode_business_record(data: bytes, pos: int, prev_osm_id: int) -> tuple[dict, int]:
    """Decode a single business record from a PTILESB block (v1/v3 compatible).

    Handles v3 extended attributes by skipping over them.
    Returns (record_dict, bytes_consumed).
    """
    start_pos = pos

    # record length prefix
    rec_len = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    rec_end = pos + rec_len

    # osm_id delta (zigzag varint)
    delta_raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + ((delta_raw >> 1) ^ -(delta_raw & 1))

    # lon/lat micro
    lon_micro = struct.unpack_from("<i", data, pos)[0]
    pos += 4
    lat_micro = struct.unpack_from("<i", data, pos)[0]
    pos += 4

    # name (u16_str)
    name, consumed = decode_string_u16(data, pos)
    pos += consumed

    # category_idx
    category_idx = data[pos]
    pos += 1

    # flags
    flags = data[pos]
    pos += 1

    # Optional fields based on flags
    phone = ""
    website = ""
    address = ""
    brand = ""

    if flags & 0x01:
        phone, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x02:
        website, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x04:
        address, consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x08:
        brand, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x10:
        # operating_status is 2-bit, no extra field length
        pass
    if flags & 0x20:
        # emails (u8_str)
        slen = data[pos]
        pos += 1 + slen
    if flags & 0x40:
        # socials (u8_str)
        slen = data[pos]
        pos += 1 + slen
    if flags & 0x80:
        # chain_count (u8) — v3 addition
        pos += 1

    # v3 extended attributes: if there are remaining bytes in the record,
    # they contain ext_flags + optional source_type/source_id/confidence/uid_alt.
    # We skip over them to stay compatible.
    remaining = rec_end - pos
    if remaining > 0:
        # ext_flags (u16)
        ext_flags = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if ext_flags & 0x01:  # source_type (u8)
            pos += 1
        if ext_flags & 0x02:  # source_id (u16_str)
            slen = struct.unpack_from("<H", data, pos)[0]
            pos += 2 + slen
        if ext_flags & 0x04:  # confidence (u8)
            pos += 1
        if ext_flags & 0x08:  # uid_alt (u64)
            pos += 8

    record = {
        "name": name,
        "lon_micro": lon_micro,
        "lat_micro": lat_micro,
        "osm_id": osm_id,
        "category_idx": category_idx,
        "flags": flags,
        "phone": phone,
        "website": website,
        "brand": brand,
    }

    return record, pos - start_pos


def decode_business_record_v4(
    data: bytes, pos: int, cen_lon: int, cen_lat: int
) -> tuple[dict, int]:
    """Decode a single v4 business record — sequential uid, i16 coords, no prefix.
    Returns (record_dict, bytes_consumed).
    """
    start_pos = pos

    # Sequential uid (zigzag varint, not delta)
    raw_id, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = (raw_id >> 1) ^ -(raw_id & 1)

    # Cell-relative i16 coordinates
    lon_off = struct.unpack_from("<h", data, pos)[0]
    lat_off = struct.unpack_from("<h", data, pos + 2)[0]
    pos += 4
    lon_micro = cen_lon + lon_off
    lat_micro = cen_lat + lat_off

    # name (u16_str)
    name, consumed = decode_string_u16(data, pos)
    pos += consumed

    # category_idx
    category_idx = data[pos]
    pos += 1

    # flags
    flags = data[pos]
    pos += 1

    phone = ""
    website = ""
    address = ""
    brand = ""

    if flags & 0x01:
        phone, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x02:
        website, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x04:
        address, consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x08:
        brand, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x10:
        pass  # amenities block — skip (not needed for name index)
    if flags & 0x80:
        # chain_count
        pos += 1

    # v4 extended attrs (source_type, source_id, confidence, star_rating, etc.)
    # All optional, signaled by a u16 ext_flags prefix if any are present.
    # ponytail: check remaining bytes — if pos < len(data) and it looks like ext_flags, parse it
    # Otherwise, we're at end of record.
    remaining = len(data) - pos
    if remaining >= 2:
        ext_flags = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if ext_flags & 0x01:
            pos += 1  # source_type u8
        if ext_flags & 0x02:
            slen = struct.unpack_from("<H", data, pos)[0]
            pos += 2 + slen
        if ext_flags & 0x04:
            pos += 1  # confidence u8
        # 0x10: star_rating u8
        if ext_flags & 0x10:
            pos += 1
        # 0x20: opening_hours block — skip
        if ext_flags & 0x20:
            rcount = data[pos]
            pos += 1 + rcount * 5
        # 0x40: source_version u32
        if ext_flags & 0x40:
            pos += 4
        # 0x80: updated u32
        if ext_flags & 0x80:
            pos += 4

    record = {
        "name": name,
        "lon_micro": lon_micro,
        "lat_micro": lat_micro,
        "osm_id": osm_id,
        "category_idx": category_idx,
        "flags": flags,
        "phone": phone,
        "website": website,
        "brand": brand,
    }

    return record, pos - start_pos


def encode_name_record(record: dict, uid: int) -> bytes:
    """Encode a business record in the name index format (subset of fields)."""
    buf = bytearray()

    # name (u16_str)
    buf.extend(encode_string_u16(record["name"]))

    # lat_micro (i32)
    buf.extend(struct.pack("<i", record["lat_micro"]))

    # lon_micro (i32)
    buf.extend(struct.pack("<i", record["lon_micro"]))

    # uid (u32) — incrementing cross-reference ID
    buf.extend(struct.pack("<I", uid))

    # category_idx (u8)
    buf.append(record["category_idx"])

    # flags (u8) — only subset of flags preserved
    flags = record["flags"]
    # Keep only phone(0x01), website(0x02), brand(0x08)
    filtered_flags = flags & (0x01 | 0x02 | 0x08)
    buf.append(filtered_flags)

    # Optional fields
    if filtered_flags & 0x01:
        buf.extend(encode_string_u8(record["phone"]))
    if filtered_flags & 0x02:
        buf.extend(encode_string_u8(record["website"]))
    if filtered_flags & 0x08:
        buf.extend(encode_string_u8(record["brand"]))

    # Prepend record length
    body = bytes(buf)
    return struct.pack("<I", len(body)) + body


def main():
    if len(sys.argv) < 2:
        print("Usage: python build_business_name_index.py <STATE>")
        sys.exit(1)

    state = sys.argv[1].upper()
    # The business file carries its own version in the name ({ST}.business_v4.ptiles),
    # which moves independently of this index's version, so glob for it rather than
    # hardcode a number here that would silently stop matching on the next bump.
    candidates = sorted(glob.glob(os.path.join(DATA_DIR, f"{state}.business_v*.ptiles")))
    if len(candidates) != 1:
        print(f"ERROR: expected exactly one {state}.business_v*.ptiles in {DATA_DIR}, "
              f"found {len(candidates)}: {candidates}")
        sys.exit(1)
    input_path = candidates[0]
    output_path = os.path.join(DATA_DIR, f"{state}.business_name_index.ptiles")

    print(f"Reading {input_path}...", flush=True)

    # --- Read input file ---
    with open(input_path, "rb") as f:
        header = read_header(f)
        print(f"  Magic: {header['magic']!r}, Version: {header['version']}", flush=True)
        print(
            f"  Features: {header['feature_count']}, Blocks: {header['block_count']}",
            flush=True,
        )

        # Read dictionary
        f.seek(header["dict_offset"])
        dict_data = f.read(header["dict_length"])
        print(f"  Dictionary: {len(dict_data):,} bytes", flush=True)

        # Read index
        f.seek(header["index_offset"])
        index_data = f.read(header["index_length"])
        index_entries = read_index(index_data)
        print(f"  Index entries: {len(index_entries)}", flush=True)

        # Read and decompress all blocks, extract records grouped by name key
        # key -> list of encoded records
        grouped: dict[int, list[bytes]] = defaultdict(list)
        total_businesses = 0
        uid_counter = 0
        is_v4 = header["version"] >= 4

        for block_idx, entry in enumerate(index_entries):
            block_offset = entry["block_offset"]
            block_length = entry["block_length"]
            feature_count = entry["feature_count"]

            f.seek(block_offset)
            compressed = f.read(block_length)

            raw = decompress_block(compressed, dict_data)

            # Decode records
            pos = 0
            prev_osm_id = 0
            block_records = 0
            if is_v4:
                # v4: sequential uid, i16 coords, no record_len, feature_count records
                import h3

                cell_int = entry["h3_cell"]
                cell_hex = format(cell_int, "x")
                clat, clon = h3.cell_to_latlng(cell_hex)
                from encoding import coord_to_micro

                cen_lon = coord_to_micro(clon)
                cen_lat = coord_to_micro(clat)
                for _ in range(feature_count):
                    if pos >= len(raw):
                        break
                    record, consumed = decode_business_record_v4(
                        raw, pos, cen_lon, cen_lat
                    )
                    pos += consumed
                    key = name_to_key(record["name"])
                    encoded = encode_name_record(record, uid_counter)
                    grouped[key].append(encoded)
                    uid_counter += 1
                    block_records += 1
            else:
                while pos < len(raw):
                    record, consumed = decode_business_record(raw, pos, prev_osm_id)
                    prev_osm_id = record["osm_id"]

                    key = name_to_key(record["name"])
                    encoded = encode_name_record(record, uid_counter)
                    grouped[key].append(encoded)

                    uid_counter += 1
                    block_records += 1
                    pos += consumed

            total_businesses += block_records

            if (
                (block_idx + 1) % 50 == 0
                or block_idx == 0
                or block_idx == len(index_entries) - 1
            ):
                print(
                    f"  Block {block_idx + 1}/{len(index_entries)}: {block_records} records, "
                    f"{total_businesses} total so far",
                    flush=True,
                )

    print(f"\nTotal businesses processed: {total_businesses}", flush=True)
    print(f"Unique name keys: {len(grouped)}", flush=True)

    # --- Compress blocks (zstd, no dictionary needed) ---
    print("\nCompressing blocks...", flush=True)

    import zstandard as zstd

    cctx = zstd.ZstdCompressor(level=12)

    sorted_keys = sorted(grouped.keys())
    compressed_blocks: dict[int, bytes] = {}
    for key in sorted_keys:
        block_data = b"".join(grouped[key])
        compressed_blocks[key] = cctx.compress(block_data)

    # --- Write output PTILESX file ---
    print(f"\nWriting {output_path}...", flush=True)

    # Build index entries
    index_entries_out = []
    running_offset = 0
    for key in sorted_keys:
        cb = compressed_blocks[key]
        entry = {
            "h3_cell": key,  # reuse h3_cell field for letter key
            "block_offset": running_offset,
            "block_length": len(cb),
            "feature_count": len(grouped[key]),
        }
        index_entries_out.append(entry)
        running_offset += len(cb)

    # Compute offsets
    dict_offset = HEADER_SIZE
    dict_length = 0  # no dictionary for name index
    index_offset = dict_offset
    index_length = 4 + len(index_entries_out) * INDEX_ENTRY_SIZE
    blocks_offset = index_offset + index_length

    with open(output_path, "wb") as f:
        # Write header
        write_header(
            f,
            MAGIC,
            VERSION,
            0.0,
            0.0,
            0.0,
            0.0,  # no meaningful bbox for name index
            total_businesses,
            len(index_entries_out),
            dict_offset,
            dict_length,
            index_offset,
            index_length,
            blocks_offset,
        )

        # Write index (no dictionary)
        from shared import write_index

        write_index(f, index_entries_out)

        # Write compressed blocks
        for entry in index_entries_out:
            f.write(compressed_blocks[entry["h3_cell"]])

    # Update index entries to use absolute offsets (same as build_business.py pattern)
    with open(output_path, "r+b") as f:
        idx_pos = index_offset + 4  # skip entry count
        for entry in index_entries_out:
            abs_offset = blocks_offset + entry["block_offset"]
            f.seek(idx_pos + 8)
            f.write(abs_offset.to_bytes(6, "little"))
            idx_pos += INDEX_ENTRY_SIZE

    total_size = os.path.getsize(output_path)

    # --- Print summary ---
    print(f"\n{state}.business_name_index.ptiles built:", flush=True)
    for key in sorted_keys:
        letter = chr(ord("a") + key) if key < 26 else ("#" if key == 26 else "?")
        count = len(grouped[key])
        print(f"  {letter}: {count} businesses", flush=True)

    print(
        f"  Total: {total_businesses} businesses across {len(sorted_keys)} keys",
        flush=True,
    )
    print(
        f"  File size: {total_size:,} bytes ({total_size / 1024 / 1024:.1f} MB)",
        flush=True,
    )


if __name__ == "__main__":
    main()
