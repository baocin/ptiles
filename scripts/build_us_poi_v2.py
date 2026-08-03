#!/usr/bin/env python3
"""
Build US POI .ptiles files from per-state pre-extracted parquet files.

Reads per-state parquet files (extracted via DuckDB from the merged US file),
performs fuzzy cross-source dedup via RapidFuzz, adds brand + chain_count
from name index, and writes PTILESB v3 format files.

Usage:
    # Single state (reads from /mnt/core/poi_state_extracts/TN.parquet)
    uv run --with pyarrow --with h3 --with zstandard --with numpy --with rapidfuzz \\
        python3 build_us_poi_v2.py --state TN

    # Full US (iterates all states)
    uv run --with pyarrow --with h3 --with zstandard --with numpy --with rapidfuzz \\
        python3 build_us_poi_v2.py --full-us

    # With dedup enabled (default, but slow on large states)
    uv run <deps> python3 build_us_poi_v2.py --state TN --no-dedup
"""

import sys

sys.stdout.reconfigure(line_buffering=True)
import os
import struct
import json
import time
import hashlib
import re
from collections import defaultdict

import pyarrow.parquet as pq
import h3

sys.path.insert(0, os.path.dirname(__file__))
from shared import (
    write_header,
    HEADER_SIZE,
    encode_varint,
    encode_string_u8,
    encode_string_u16,
    zigzag_encode,
    compress_block,
    train_dictionary,
)
from encoding import coord_to_micro
from states import get_state

try:
    from rapidfuzz import fuzz

    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False
    print("WARNING: rapidfuzz not installed. Fuzzy dedup disabled.", flush=True)

MAGIC = b"PTILESB\0"
VERSION = 3
H3_RES = 7

SRC_OSM = 0
SRC_OVERTURE = 1
SRC_FOURSQUARE = 2
SRC_CUSTOM = 3

STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
]

STATE_EXTRACT_DIR = "/mnt/core/poi_state_extracts"
CHAIN_INDEX_PATH = "/mnt/core/poi_chain_index.parquet"


# =========================================================================
# Helpers
# =========================================================================


def make_unified_id(source: str, src_id: str, use_short: bool = False) -> int:
    key = f"{source}:{src_id}".encode()
    if use_short:
        return int.from_bytes(hashlib.sha256(key).digest()[:4], "little")
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "little")


def normalize_name(n: str) -> str:
    n = n.lower().strip()
    n = re.sub(r"[^a-z0-9 ]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# =========================================================================
# Load brand map + chain index
# =========================================================================


def load_brand_map(path: str = "/tmp/overture_brand_map.parquet") -> dict[str, str]:
    if not os.path.exists(path):
        print(f"  (no brand map at {path})", flush=True)
        return {}
    t = pq.read_table(path, columns=["overture_id", "brand_name"])
    ids = t.column("overture_id").to_pylist()
    names = t.column("brand_name").to_pylist()
    m = dict(zip(ids, names))
    print(f"  Brand map: {len(m):,} entries loaded", flush=True)
    return m


def load_chain_index(path: str = CHAIN_INDEX_PATH) -> dict[str, int]:
    """Load compact chain index: name_lower -> total_count.

    Only names with >=2 occurrences. Cached at CHAIN_INDEX_PATH.
    """
    if not os.path.exists(path):
        print(f"  (no chain index at {path})", flush=True)
        return {}
    t = pq.read_table(path, columns=["name", "count"])
    names = t.column("name").to_pylist()
    counts = t.column("count").to_pylist()
    m = {}
    for i in range(len(names)):
        n = names[i]
        c = counts[i]
        if n and c:
            m[n.lower()] = int(c)
    print(f"  Chain index: {len(m):,} entries loaded", flush=True)
    return m


# =========================================================================
# Load per-state parquet
# =========================================================================


def load_state_file(state: str, brand_map: dict, chain_idx: dict) -> list[dict]:
    """Load a pre-extracted state parquet file and build record dicts."""
    path = os.path.join(STATE_EXTRACT_DIR, f"{state}.parquet")
    if not os.path.exists(path):
        print(f"  State file not found: {path}", flush=True)
        return []

    t = pq.read_table(
        path,
        columns=[
            "source",
            "source_id",
            "name",
            "lat",
            "lon",
            "primary_category",
            "address",
            "city",
            "phone",
            "website",
            "confidence",
        ],
    )

    src = t.column("source").to_pylist()
    ids = t.column("source_id").to_pylist()
    names = t.column("name").to_pylist()
    lats = t.column("lat").to_pylist()
    lons = t.column("lon").to_pylist()
    cats = t.column("primary_category").to_pylist()
    addrs = t.column("address").to_pylist()
    cities = t.column("city").to_pylist()
    phones = t.column("phone").to_pylist()
    websites = t.column("website").to_pylist()
    confs = t.column("confidence").to_pylist()

    # ponytail: the per-state extracts are cut with `WHERE state = '{ST}'` on the
    # vendor address column (Overture addresses[1].region, Foursquare region) and
    # that column is never cross-checked against lat/lon. ~0.2% of rows carry a
    # correct state string but garbage upstream coordinates — a Memphis hotel at
    # (59.59,-68.09) in Quebec, a Nashville shop in Siberia, London's Tobacco Dock
    # tagged city=Tiptonville — so they get H3-indexed into cells nowhere near the
    # state and pollute that state's file. Coordinates are what a .ptiles file is
    # indexed by, so a row we can't place is worse than a row we drop. Same padded
    # bbox guard build_parks/build_places/extract_all_states already apply.
    bbox = get_state(state)
    skipped_bbox = 0

    records = []
    for i in range(len(t)):
        lat, lon = lats[i], lons[i]
        if lat is None or lon is None:
            skipped_bbox += 1
            continue
        if bbox and not (
            bbox.min_lon <= lon <= bbox.max_lon and bbox.min_lat <= lat <= bbox.max_lat
        ):
            skipped_bbox += 1
            continue

        s = src[i]
        if s == "foursquare":
            source_type = SRC_FOURSQUARE
            source_name = "foursquare"
        else:
            source_type = SRC_OVERTURE
            source_name = "overture"

        src_id = ids[i]
        uid = make_unified_id(source_name, src_id)
        name = names[i] or ""
        primary_cat = cats[i] or ""
        confidence = (
            round(confs[i] * 100) if confs[i] is not None and confs[i] > 0 else 0
        )

        brand = ""
        if source_type == SRC_OVERTURE and brand_map and src_id in brand_map:
            brand = brand_map[src_id]

        chain_count = 0
        name_lower = name.lower()
        if chain_idx and name_lower in chain_idx:
            chain_count = chain_idx[name_lower]

        rec = {
            "unified_id": uid,
            "unified_id_alt": 0,
            "source_type": source_type,
            "source_id": src_id,
            "lon": lons[i],
            "lat": lats[i],
            "name": name,
            "primary_category": primary_cat,
            "phone": phones[i] or "",
            "website": websites[i] or "",
            "address": addrs[i] or "",
            "city": cities[i] or "",
            "confidence": min(confidence, 100),
            "brand": brand,
            "chain_count": chain_count,
            "operating_status": "",
            "emails": "",
            "socials": "",
        }
        rec["_source_type"] = source_type
        records.append(rec)

    if skipped_bbox:
        print(
            f"  Dropped {skipped_bbox:,} rows outside the {state} bbox (bad upstream coords)",
            flush=True,
        )
    print(f"  Loaded {len(records):,} records from {state}.parquet", flush=True)
    return records


# =========================================================================
# Fuzzy dedup
# =========================================================================


def fuzzy_dedup(records: list) -> list:
    if len(records) < 2:
        return records

    for r in records:
        r["_src_key"] = (
            "foursquare" if r.get("_source_type") == SRC_FOURSQUARE else "overture"
        )

    grid = defaultdict(list)
    for i, r in enumerate(records):
        key = (round(r["lon"], 3), round(r["lat"], 3))
        grid[key].append(i)

    overture_to_unified: dict[int, int] = {}
    matched_fs_ids: set[int] = set()

    if HAS_RAPIDFUZZ:
        for coord, indices in grid.items():
            if len(indices) < 2:
                continue
            fs_i = [i for i in indices if records[i]["_src_key"] == "foursquare"]
            ov_i = [i for i in indices if records[i]["_src_key"] == "overture"]
            if not fs_i or not ov_i:
                continue

            for fi in fs_i:
                fn = normalize_name(records[fi].get("name", ""))
                if not fn or len(fn) < 3:
                    continue
                for oi in ov_i:
                    on = normalize_name(records[oi].get("name", ""))
                    if not on or len(on) < 3:
                        continue
                    ratio = fuzz.ratio(fn, on) / 100.0
                    if ratio >= 0.85:
                        overture_to_unified[oi] = records[fi]["unified_id"]
                        matched_fs_ids.add(records[fi]["unified_id"])
                        continue
                    words_fs = set(fn.split())
                    words_ov = set(on.split())
                    common = words_fs & words_ov
                    if len(common) >= 1 and len(common) == min(
                        len(words_fs), len(words_ov)
                    ):
                        overture_to_unified[oi] = records[fi]["unified_id"]
                        matched_fs_ids.add(records[fi]["unified_id"])
                        continue
                    tok_set = fuzz.token_set_ratio(fn, on) / 100.0
                    if tok_set >= 0.85:
                        overture_to_unified[oi] = records[fi]["unified_id"]
                        matched_fs_ids.add(records[fi]["unified_id"])
                        continue
                    fc = records[fi].get("primary_category", "")
                    oc = records[oi].get("primary_category", "")
                    cat_ok = fc and oc and (fc in oc or oc in fc)
                    if ratio >= 0.75 and cat_ok:
                        overture_to_unified[oi] = records[fi]["unified_id"]
                        matched_fs_ids.add(records[fi]["unified_id"])
                        continue

    for ov_idx, fs_uid in overture_to_unified.items():
        records[ov_idx]["unified_id"] = fs_uid
        records[ov_idx]["unified_id_alt"] = records[ov_idx].get("unified_id_alt", 0)

    merged = len(overture_to_unified)
    print(f"  Fuzzy dedup: {merged} cross-source merges applied", flush=True)
    return records


# =========================================================================
# Category index
# =========================================================================


def build_category_index(records: list[dict]) -> tuple[dict[str, int], list[str]]:
    cat_counts = defaultdict(int)
    for r in records:
        if r["primary_category"]:
            cat_counts[r["primary_category"]] += 1
    sorted_cats = sorted(cat_counts.items(), key=lambda x: -x[1])
    idx = {}
    rev = []
    for i, (cat, _) in enumerate(sorted_cats[:254]):
        idx[cat] = i + 1
        rev.append(cat)
    print(f"  Category index: {len(idx)} entries", flush=True)
    return idx, rev


# =========================================================================
# Record encoding (v3)
# =========================================================================


def encode_record_v3(rec: dict, prev_unified_id: int, cat_index: dict) -> bytes:
    buf = bytearray()

    delta = rec["unified_id"] - prev_unified_id
    buf.extend(encode_varint(zigzag_encode(delta)))

    buf.extend(struct.pack("<i", coord_to_micro(rec["lon"])))
    buf.extend(struct.pack("<i", coord_to_micro(rec["lat"])))

    buf.extend(encode_string_u16(rec["name"]))

    cat = rec.get("primary_category", "")
    buf.append(cat_index.get(cat, 0))

    flags = 0
    phone = rec.get("phone", "")
    website = rec.get("website", "")
    address = rec.get("address", "")
    brand = rec.get("brand", "")
    status = rec.get("operating_status", "")
    email = rec.get("emails", "")
    social = rec.get("socials", "")
    chain_count = rec.get("chain_count", 0)

    if phone:
        flags |= 0x01
    if website:
        flags |= 0x02
    if address:
        flags |= 0x04
    if brand:
        flags |= 0x08
    if status == "permanently_closed":
        flags |= 0x10
    elif status == "temporarily_closed":
        flags |= 0x12
    if email:
        flags |= 0x20
    if social:
        flags |= 0x40
    if chain_count > 0:
        flags |= 0x80  # NEW: bit 7 = chain with known count

    buf.append(flags)

    if phone:
        buf.extend(encode_string_u8(phone))
    if website:
        buf.extend(encode_string_u8(website))
    if address:
        buf.extend(encode_string_u16(address))
    if brand:
        buf.extend(encode_string_u8(brand))
    if email:
        buf.extend(encode_string_u8(email))
    if social:
        buf.extend(encode_string_u8(social))

    # NEW: if chain flag set, encode chain count as u8
    if chain_count > 0:
        chain_val = min(chain_count, 255)
        buf.append(chain_val)

    # Extended attrs (v3)
    source_type = rec.get("source_type", SRC_OSM)
    source_id = rec.get("source_id", "")
    confidence = rec.get("confidence", 0)
    uid_alt = rec.get("unified_id_alt", 0)

    has_ext = source_type != SRC_OSM or source_id or confidence or uid_alt
    if has_ext:
        ext_flags = 0
        if source_type != SRC_OSM:
            ext_flags |= 0x01
        if source_id:
            ext_flags |= 0x02
        if confidence:
            ext_flags |= 0x04
        if uid_alt:
            ext_flags |= 0x08

        buf.extend(struct.pack("<H", ext_flags))
        if ext_flags & 0x01:
            buf.append(source_type)
        if ext_flags & 0x02:
            buf.extend(encode_string_u16(source_id))
        if ext_flags & 0x04:
            buf.append(min(confidence, 255))
        if ext_flags & 0x08:
            buf.extend(struct.pack("<Q", uid_alt))

    record_body = bytes(buf)
    return struct.pack("<I", len(record_body)) + record_body


# =========================================================================
# Group by H3 cell and encode
# =========================================================================


def group_and_encode(records: list[dict], cat_index: dict) -> dict[int, bytes]:
    cells: dict[int, list[dict]] = defaultdict(list)
    for r in records:
        cell = h3.latlng_to_cell(r["lat"], r["lon"], H3_RES)
        if isinstance(cell, str):
            cell = int(cell, 16)
        cell = int(cell)
        cells[cell].append(r)
    print(f"  Grouped into {len(cells)} H3 cells", flush=True)

    sorted_cells = sorted(cells.items(), key=lambda x: x[0])
    blocks = {}
    for cell, cell_records in sorted_cells:
        cell_records.sort(key=lambda r: r["unified_id"])
        buf = bytearray()
        prev_uid = 0
        for r in cell_records:
            buf.extend(encode_record_v3(r, prev_uid, cat_index))
            prev_uid = r["unified_id"]
        blocks[int(cell)] = bytes(buf)
    return blocks


# =========================================================================
# Write PTILESB v3 file
# =========================================================================


def write_ptilesb(
    output_path: str,
    records: list[dict],
    blocks: dict[int, bytes],
    cat_reverse: list[str],
):
    print(f"Writing {output_path}...", flush=True)

    samples = (
        list(blocks.values())[:1000] if len(blocks) > 1000 else list(blocks.values())
    )
    print(f"  Training zstd dictionary on {len(samples)} samples...", flush=True)
    dict_data = train_dictionary(samples)

    compressed_blocks: dict[int, bytes] = {}
    for cell, raw in blocks.items():
        compressed_blocks[cell] = compress_block(raw, dict_data)

    sorted_cells = sorted(compressed_blocks.keys())

    index_entries = []
    running_offset = 0
    for cell in sorted_cells:
        cb = compressed_blocks[cell]
        entry = {
            "h3_cell": cell,
            "block_offset": running_offset,
            "block_length": len(cb),
            "feature_count": sum(
                1
                for r in records
                if h3.latlng_to_cell(r["lat"], r["lon"], H3_RES) == cell
            ),
        }
        index_entries.append(entry)
        running_offset += len(cb)

    header_size = HEADER_SIZE
    dict_offset = header_size
    dict_length = len(dict_data)
    index_offset = dict_offset + dict_length
    index_length = 4 + len(index_entries) * 19
    blocks_offset = index_offset + index_length

    lats = [r["lat"] for r in records]
    lons = [r["lon"] for r in records]

    with open(output_path, "wb") as f:
        write_header(
            f,
            MAGIC,
            VERSION,
            min(lats),
            min(lons),
            max(lats),
            max(lons),
            len(records),
            len(compressed_blocks),
            dict_offset,
            dict_length,
            index_offset,
            index_length,
            blocks_offset,
        )
        f.write(dict_data)
        from shared import write_index

        write_index(f, index_entries)
        for entry in index_entries:
            f.write(compressed_blocks[entry["h3_cell"]])

    # Fix up absolute offsets in index
    idx_offset = index_offset
    with open(output_path, "r+b") as f:
        idx_pos = idx_offset + 4
        for entry in index_entries:
            abs_offset = blocks_offset + entry["block_offset"]
            f.seek(idx_pos + 8)
            f.write(abs_offset.to_bytes(6, "little"))
            idx_pos += 19

    total_size = os.path.getsize(output_path)
    print(
        f"  Size: {total_size:,} bytes ({total_size / 1024 / 1024:.1f} MB)", flush=True
    )
    print(f"  POIs: {len(records)}", flush=True)
    print(f"  Cells: {len(compressed_blocks)}", flush=True)
    print(f"  Dictionary: {dict_length:,} bytes", flush=True)

    meta_path = output_path.replace(".ptiles", "_categories.json")
    with open(meta_path, "w") as f:
        json.dump(
            {
                "categories": cat_reverse,
                "total_places": len(records),
                "version": 3,
                "format": "unified-poi",
                "chain_count_index": True,
            },
            f,
            indent=2,
        )
    print(f"  Category index: {meta_path}", flush=True)


# =========================================================================
# Main
# =========================================================================


def main():
    import argparse

    ap = argparse.ArgumentParser(
        description="Build US POI ptiles from per-state parquet files"
    )
    ap.add_argument(
        "--output-dir", default="/home/aoi/kino/projects/ptiles/data/states/"
    )
    ap.add_argument("--state", default="TN")
    ap.add_argument("--full-us", action="store_true")
    ap.add_argument("--no-dedup", action="store_true")
    args = ap.parse_args()

    t0 = time.time()

    print("Loading brand map...", flush=True)
    brand_map = load_brand_map()
    print("Loading chain index...", flush=True)
    chain_idx = load_chain_index()

    if args.full_us:
        states_to_build = STATES
    else:
        states_to_build = [args.state.upper()]

    for state in states_to_build:
        st_t0 = time.time()
        print(f"\n=== {state} ===", flush=True)

        records = load_state_file(state, brand_map, chain_idx)
        if not records:
            print(f"  No records for {state}, skipping", flush=True)
            continue

        if not args.no_dedup:
            records = fuzzy_dedup(records)

        cat_index, cat_reverse = build_category_index(records)
        blocks = group_and_encode(records, cat_index)

        os.makedirs(args.output_dir, exist_ok=True)
        out_path = os.path.join(args.output_dir, f"{state}.business.ptiles")
        write_ptilesb(out_path, records, blocks, cat_reverse)

        elapsed = time.time() - st_t0
        print(f"  {state} done in {elapsed:.1f}s", flush=True)

    print(f"\nTotal time: {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
