#!/usr/bin/env python3
"""
Build US-wide business places (PTILESB) -- memory-safe two-pass approach.

Pass 1: Scan parquet files, write per-state GeoJSONL temp files (disk-backed).
Pass 2: For each state, read temp file, group by H3 cell, encode PTILESB.
"""
import sys, os, struct, io, glob, json, time, math
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
import pyarrow.parquet as pq
import h3
import numpy as np
import zstandard as zstd

from shared import (
    write_header, HEADER_SIZE, encode_index_entry,
    encode_varint, encode_string_u8, encode_string_u16,
    zigzag_encode, compress_block, train_dictionary,
)
from states import STATES, state_bbox

PLACES_DIR = "/home/aoi/overture-2026-04-15.0/places"
OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
TEMP_DIR = Path("/tmp/ptiles_business_v1")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
H3_RES = 7
MAGIC = b"PTILESB\0"
VERSION = 1

# Build state bbox lookup
STATE_BOXES = {}
for s in STATES:
    bbox = state_bbox(s)
    STATE_BOXES[s.abbr] = (bbox[0], bbox[1], bbox[2], bbox[3])

def find_state(lon, lat):
    for abbr, (min_lon, min_lat, max_lon, max_lat) in STATE_BOXES.items():
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return abbr
    return None

# --- Pass 1: Scan parquet, write per-state temp files ---

def pass1_scan():
    """Read parquet files, write per-state GeoJSONL temp files."""
    files = sorted(glob.glob(os.path.join(PLACES_DIR, "part-*.zstd.parquet")))
    if not files:
        print("ERROR: No parquet files found", flush=True)
        sys.exit(1)

    print(f"Pass 1: Scanning {len(files)} parquet files...", flush=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    
    # Open per-state temp files
    temp_files = {}
    for s in STATES:
        tf = TEMP_DIR / f"{s.abbr}.jsonl"
        temp_files[s.abbr] = open(tf, "w")

    columns = ["id", "geometry", "names", "categories", "addresses",
               "phones", "websites", "emails", "socials", "brand", "operating_status"]
    total_us = 0
    t0 = time.time()

    for fname in files:
        table = pq.read_table(fname, columns=columns)
        n = len(table)

        addr_py = table.column("addresses").to_pylist()
        geom_py = table.column("geometry").to_pylist()
        id_py = table.column("id").to_pylist()
        names_py = table.column("names").to_pylist()
        cats_py = table.column("categories").to_pylist()
        phones_py = table.column("phones").to_pylist()
        websites_py = table.column("websites").to_pylist()
        emails_py = table.column("emails").to_pylist()
        socials_py = table.column("socials").to_pylist()
        brand_py = table.column("brand").to_pylist()
        op_py = table.column("operating_status").to_pylist()

        file_count = 0
        for i in range(n):
            addrs = addr_py[i]
            if not addrs:
                continue
            a0 = addrs[0]
            if a0.get("country", "") != "US":
                continue
            geom_bytes = geom_py[i]
            if len(geom_bytes) < 21:
                continue
            lon = struct.unpack_from("<d", geom_bytes, 5)[0]
            lat = struct.unpack_from("<d", geom_bytes, 13)[0]
            st = find_state(lon, lat)
            if not st:
                continue

            names = names_py[i]
            cats = cats_py[i]
            phone = phones_py[i][0] if phones_py[i] else ""
            website = websites_py[i][0] if websites_py[i] else ""
            email = emails_py[i][0] if emails_py[i] else ""
            social = socials_py[i][0] if socials_py[i] else ""
            brand = ""
            br = brand_py[i]
            if br:
                bn = br.get("names", {})
                if bn:
                    primary = bn.get("primary", "")
                    if primary:
                        brand = primary
            freeform = a0.get("freeform", "") or ""

            place = {
                "id": str(id_py[i]),
                "lon": round(lon, 6), "lat": round(lat, 6),
                "name": names.get("primary", "") if names else "",
                "cat": cats.get("primary", "") if cats else "",
                "addr": freeform,
                "phone": phone, "web": website,
                "email": email, "social": social,
                "brand": brand, "op": op_py[i] or "",
            }
            temp_files[st].write(json.dumps(place, ensure_ascii=False) + "\n")
            file_count += 1
            total_us += 1

        basename = os.path.basename(fname)
        print(f"  {basename}: {file_count} US places ({total_us} total)", flush=True)
        
        # Free memory
        del addr_py, geom_py, id_py, names_py, cats_py
        del phones_py, websites_py, emails_py, socials_py, brand_py, op_py
        del table
        gc = __import__('gc')
        gc.collect()

    # Close all temp files
    for f in temp_files.values():
        f.close()

    dt = time.time() - t0
    print(f"\nPass 1 complete: {total_us} US places in {dt:.0f}s", flush=True)
    
    # Count per state
    state_counts = {}
    for st in STATE_BOXES:
        tf = TEMP_DIR / f"{st}.jsonl"
        if tf.exists():
            cnt = sum(1 for _ in open(tf))
            if cnt > 0:
                state_counts[st] = cnt
    print(f"States with places: {len(state_counts)}", flush=True)
    for st, cnt in sorted(state_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"  {st}: {cnt:,}", flush=True)
    return state_counts

# --- Pass 2: Encode per-state PTILESB files ---

def encode_record(place, cat_index):
    buf = bytearray()
    osm_id = abs(hash(place["id"])) & 0x7FFFFFFFFFFFFFFF
    buf.extend(encode_varint(zigzag_encode(osm_id)))
    buf.extend(struct.pack("<i", round(place["lon"] * 100_000)))
    buf.extend(struct.pack("<i", round(place["lat"] * 100_000)))
    buf.extend(encode_string_u16(place["name"]))

    cat = place.get("cat", "")
    buf.append(cat_index.get(cat, 0))

    flags = 0
    phone = place.get("phone", "")
    website = place.get("web", "")
    address = place.get("addr", "")
    brand = place.get("brand", "")
    status = place.get("op", "")
    email = place.get("email", "")
    social = place.get("social", "")
    if phone:   flags |= 0x01
    if website: flags |= 0x02
    if address: flags |= 0x04
    if brand:   flags |= 0x08
    if status == "permanently_closed":    flags |= 0x10
    elif status == "temporarily_closed":  flags |= 0x12
    if email:   flags |= 0x20
    if social:  flags |= 0x40
    buf.append(flags)
    if phone:   buf.extend(encode_string_u8(phone))
    if website: buf.extend(encode_string_u8(website))
    if address: buf.extend(encode_string_u16(address))
    if brand:   buf.extend(encode_string_u8(brand))
    if email:   buf.extend(encode_string_u8(email))
    if social:  buf.extend(encode_string_u8(social))

    body = bytes(buf)
    return struct.pack("<I", len(body)) + body

def pass2_encode(state_counts):
    """Read per-state temp files, encode PTILESB, upload to R2."""
    print(f"\nPass 2: Encoding {len(state_counts)} states...", flush=True)
    total_places = 0
    total_bytes = 0
    t0 = time.time()

    for st in sorted(state_counts.keys(), key=lambda x: -state_counts[x]):
        tf = TEMP_DIR / f"{st}.jsonl"
        print(f"\n=== {st}: {state_counts[st]:,} places ===", flush=True)
        t1 = time.time()

        # Read all places for this state
        places = []
        with open(tf) as f:
            for line in f:
                places.append(json.loads(line))

        if not places:
            continue

        # Build category index
        cat_counts = defaultdict(int)
        for p in places:
            if p.get("cat"):
                cat_counts[p["cat"]] += 1
        sorted_cats = sorted(cat_counts.items(), key=lambda x: -x[1])
        cat_index = {}
        cat_list = []
        for i, (cat, _) in enumerate(sorted_cats[:254]):
            cat_index[cat] = i + 1
            cat_list.append(cat)

        # Group by H3 cell
        cells = defaultdict(list)
        for p in places:
            cell_str = h3.latlng_to_cell(p["lat"], p["lon"], H3_RES)
            cells[int(cell_str, 16)].append(p)
        print(f"  H3 cells: {len(cells)}", flush=True)

        # Encode records per cell (no per-cell whole-block encoding, just raw records)
        sorted_cells = sorted(cells.keys())
        raw_blocks = {}
        for cell in sorted_cells:
            buf = bytearray()
            for p in cells[cell]:
                buf.extend(encode_record(p, cat_index))
            raw_blocks[cell] = bytes(buf)

        # Train dict + compress
        samples = list(raw_blocks.values())[:2000]
        dict_data = train_dictionary(samples)
        compressed = {c: compress_block(b, dict_data) for c, b in raw_blocks.items()}

        # Header layout
        dict_offset = HEADER_SIZE
        dict_length = len(dict_data)
        index_length = 4 + len(sorted_cells) * 19
        index_offset = dict_offset + dict_length
        blocks_offset = index_offset + index_length

        all_lats = [h3.cell_to_latlng(hex(c)[2:])[0] for c in sorted_cells]
        all_lons = [h3.cell_to_latlng(hex(c)[2:])[1] for c in sorted_cells]

        out_path = OUTPUT_DIR / f"{st}.business.ptiles"
        with open(out_path, "wb") as f:
            write_header(f, MAGIC, VERSION, min(all_lats), min(all_lons),
                         max(all_lats), max(all_lons), len(places), len(compressed),
                         dict_offset, dict_length, index_offset, index_length, blocks_offset)
            f.seek(dict_offset); f.write(dict_data)
            f.seek(index_offset)
            f.write(struct.pack("<I", len(sorted_cells)))
            for i, cell in enumerate(sorted_cells):
                cb = compressed[cell]
                blk_off = sum(len(compressed[c]) for c in sorted_cells[:i])
                # Use standard 19-byte index entry format
                f.write(encode_index_entry(cell, blk_off, len(cb), len(cells[cell])))
            f.seek(blocks_offset)
            for cell in sorted_cells:
                f.write(compressed[cell])

        cat_path = OUTPUT_DIR / f"{st}.business_categories.json"
        with open(cat_path, "w") as f:
            json.dump({"categories": cat_list}, f)

        dt = time.time() - t1
        sz = out_path.stat().st_size
        print(f"  {sz:,} bytes in {dt:.0f}s", flush=True)
        total_places += len(places)
        total_bytes += sz

    total_time = time.time() - t0
    print(f"\n=== SUMMARY ===", flush=True)
    print(f"  States: {len(state_counts)}", flush=True)
    print(f"  Places: {total_places:,}", flush=True)
    print(f"  Size:   {total_bytes:,} bytes ({total_bytes/1024/1024:.1f} MB)", flush=True)
    print(f"  Time:   {total_time:.0f}s", flush=True)

def main():
    print("=== US Business PTILESB ===", flush=True)
    print(f"Temp: {TEMP_DIR}", flush=True)
    print(f"Output: {OUTPUT_DIR}", flush=True)
    print(f"States: {len(STATE_BOXES)}", flush=True)
    print(flush=True)
    
    # Clean temp from previous run
    if TEMP_DIR.exists():
        import shutil
        shutil.rmtree(TEMP_DIR)
    
    state_counts = pass1_scan()
    if state_counts:
        pass2_encode(state_counts)
        print(f"\nUpload: AWS_PROFILE=mdt-r2 aws s3 cp {OUTPUT_DIR}/*.business* s3://mydatatimeline/maps/", flush=True)

if __name__ == "__main__":
    main()
