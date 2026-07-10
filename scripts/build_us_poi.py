#!/usr/bin/env python3
"""
Build US POI .ptiles files from the merged Foursquare+Overture parquet.

Reads poi_lookup_tn.parquet (or per-state subsets), performs
fuzzy cross-source dedup via RapidFuzz, assigns unified_ids,
and writes PTILESB v3 format files.

Usage:
    uv run --with pyarrow --with shapely --with h3 --with zstandard --with numpy \\
        python build_us_poi.py \\
            --input /home/aoi/code/rookery/geo/poi_lookup_tn.parquet \\
            --output-dir /home/aoi/kino/projects/ptiles/data/states/ \\
            --state TN

    # Full US (iterates all states)
    uv run --with pyarrow --with shapely --with h3 --with zstandard --with numpy \\
        python build_us_poi.py --full-us
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

try:
    from rapidfuzz import fuzz

    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False
    print("WARNING: rapidfuzz not installed. Fuzzy dedup disabled.", flush=True)

MAGIC = b"PTILESB\0"
VERSION = 3
H3_RES = 7

# --- Source enum ---
SRC_OSM = 0
SRC_OVERTURE = 1
SRC_FOURSQUARE = 2
SRC_CUSTOM = 3

# US state bboxes for filtering (from OSM data boundaries)
STATE_BBOXES = {
    "AL": (30.1, -88.5, 35.0, -84.9),
    "AR": (33.0, -94.6, 36.5, -89.7),
    "AZ": (31.3, -115.0, 37.0, -109.0),
    "CA": (32.5, -124.5, 42.0, -114.1),
    "CO": (37.0, -109.1, 41.0, -102.0),
    "CT": (40.9, -73.7, 42.1, -71.8),
    "DC": (38.8, -77.1, 39.0, -76.9),
    "DE": (38.4, -75.8, 39.9, -75.0),
    "FL": (24.4, -87.6, 31.0, -80.0),
    "GA": (30.3, -85.6, 35.0, -80.8),
    "HI": (18.9, -160.3, 22.3, -154.8),
    "IA": (40.3, -96.6, 43.5, -90.1),
    "ID": (42.0, -117.3, 49.0, -111.0),
    "IL": (36.9, -91.5, 42.5, -87.5),
    "IN": (37.7, -88.1, 41.8, -84.8),
    "KS": (36.9, -102.1, 40.0, -94.6),
    "KY": (36.5, -89.6, 39.1, -81.9),
    "LA": (28.9, -94.0, 33.0, -88.8),
    "MA": (41.2, -73.5, 42.9, -69.9),
    "MD": (37.8, -79.5, 39.7, -75.0),
    "ME": (43.0, -71.1, 47.5, -66.9),
    "MI": (41.7, -90.4, 47.5, -82.1),
    "MN": (43.4, -97.2, 49.4, -89.5),
    "MO": (35.9, -95.8, 40.6, -89.1),
    "MS": (30.0, -91.7, 35.0, -88.1),
    "MT": (44.3, -116.0, 49.0, -104.0),
    "NC": (33.8, -84.3, 36.6, -75.5),
    "ND": (45.9, -104.1, 49.0, -96.6),
    "NE": (39.9, -104.1, 43.0, -95.3),
    "NH": (42.6, -72.6, 45.3, -70.7),
    "NJ": (38.8, -75.6, 41.4, -73.9),
    "NM": (31.3, -109.1, 37.0, -103.0),
    "NV": (35.0, -120.0, 42.0, -114.0),
    "NY": (40.4, -79.8, 45.0, -71.8),
    "OH": (38.4, -84.8, 42.0, -80.5),
    "OK": (33.6, -103.0, 37.0, -94.4),
    "OR": (42.0, -124.6, 46.3, -116.5),
    "PA": (39.7, -80.5, 42.3, -74.7),
    "RI": (41.1, -71.9, 42.0, -71.1),
    "SC": (32.0, -83.4, 35.2, -78.5),
    "SD": (42.4, -104.1, 46.0, -96.4),
    "TN": (34.9, -90.3, 36.7, -81.6),
    "TX": (25.8, -106.6, 36.5, -93.5),
    "UT": (37.0, -114.1, 42.0, -109.0),
    "VA": (36.5, -83.7, 39.5, -75.2),
    "VT": (42.6, -73.4, 45.0, -71.5),
    "WA": (45.5, -124.8, 49.0, -117.0),
    "WI": (42.4, -92.9, 47.0, -86.8),
    "WV": (37.2, -82.7, 40.6, -77.7),
    "WY": (41.0, -111.1, 45.0, -104.0),
    "AK": (51.2, -180.0, 71.4, -130.0),
}


# =========================================================================
# unified_id helpers
# =========================================================================


def make_unified_id(source: str, src_id: str, use_short: bool = False) -> int:
    """Deterministic u64 from SHA-256(source:id)."""
    key = f"{source}:{src_id}".encode()
    if use_short:
        # First 4 bytes as u32 (for internal cross-ref, less collision resistance)
        return int.from_bytes(hashlib.sha256(key).digest()[:4], "little")
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "little")


def normalize_name(n: str) -> str:
    """Lowercase strip punctuation, collapse whitespace."""
    n = n.lower().strip()
    n = re.sub(r"[^a-z0-9 ]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# =========================================================================
# Fuzzy dedup
# =========================================================================


def fuzzy_dedup(records: list) -> list:
    """
    Find cross-source pairs within ~110m and assign unified_ids.

    Strategy:
    - Index records by (rounded_lat, rounded_lon, ~110m grid)
    - Within each grid cell, compare Foursquare vs Overture name similarity
    - If names clearly match (ratio >= 0.85, or subset match, or tok_set >= 0.85):
      use the Foursquare-derived unified_id as canonical for both
    """
    if len(records) < 2:
        return records

    # Identify Foursquare vs Overture
    for r in records:
        r["_src_key"] = (
            "foursquare" if r.get("_source_type") == SRC_FOURSQUARE else "overture"
        )

    # Index by ~110m grid
    grid = defaultdict(list)
    for i, r in enumerate(records):
        key = (round(r["lon"], 3), round(r["lat"], 3))
        grid[key].append(i)

    # Map of overture_idx -> foursquare unified_id (where match found)
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
                        # Same business — use Foursquare unified_id as canonical
                        overture_to_unified[oi] = records[fi]["unified_id"]
                        matched_fs_ids.add(records[fi]["unified_id"])
                        continue
                    # Subset check: one name is fully contained in the other
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
                    # Category overlap helps
                    fc = records[fi].get("primary_category", "")
                    oc = records[oi].get("primary_category", "")
                    cat_ok = fc and oc and (fc in oc or oc in fc)
                    if ratio >= 0.75 and cat_ok:
                        overture_to_unified[oi] = records[fi]["unified_id"]
                        matched_fs_ids.add(records[fi]["unified_id"])
                        continue

    # Apply merges: overture records take the Foursquare unified_id
    for ov_idx, fs_uid in overture_to_unified.items():
        records[ov_idx]["unified_id"] = fs_uid
        records[ov_idx]["unified_id_alt"] = records[ov_idx].get("unified_id_alt", 0)

    merged = len(overture_to_unified)
    print(f"  Fuzzy dedup: {merged} cross-source merges applied", flush=True)

    return records


# =========================================================================
# Load parquet
# =========================================================================


def load_brand_map(path: str = "/tmp/overture_brand_map.parquet") -> dict[str, str]:
    """Load Overture brand map: overture_id -> brand_name."""
    if not os.path.exists(path):
        print(f"  (no brand map at {path})", flush=True)
        return {}
    t = pq.read_table(path, columns=["overture_id", "brand_name"])
    ids = t.column("overture_id").to_pylist()
    names = t.column("brand_name").to_pylist()
    m = dict(zip(ids, names))
    print(f"  Brand map: {len(m):,} entries loaded", flush=True)
    return m


def load_parquet(
    path: str, state_bbox: tuple = None, brand_map: dict[str, str] | None = None
) -> list[dict]:
    """Load merged POI parquet, filter to state bbox, look up brands."""
    print(f"Loading {path}...", flush=True)
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
            "state",
            "country",
            "phone",
            "website",
            "confidence",
        ],
    )
    print(f"  Total records: {len(t)}", flush=True)

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

    records = []
    skipped = 0
    for i in range(len(t)):
        lat, lon = lats[i], lons[i]
        if state_bbox:
            min_lat, min_lon, max_lat, max_lon = state_bbox
            if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
                skipped += 1
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
        primary_cat = cats[i] or ""
        confidence = (
            round(confs[i] * 100) if confs[i] is not None and confs[i] > 0 else 0
        )

        # Brand lookup: overture source_id match
        brand = ""
        if source_type == SRC_OVERTURE and brand_map and src_id in brand_map:
            brand = brand_map[src_id]

        rec = {
            "unified_id": uid,
            "unified_id_alt": 0,
            "source_type": source_type,
            "source_id": src_id,
            "lon": lon,
            "lat": lat,
            "name": names[i] or "",
            "primary_category": primary_cat,
            "phone": phones[i] or "",
            "website": websites[i] or "",
            "address": addrs[i] or "",
            "city": cities[i] or "",
            "confidence": min(confidence, 100),
            "brand": brand,
            "operating_status": "",
            "emails": "",
            "socials": "",
        }

        rec["_source_type"] = source_type
        records.append(rec)

    print(f"  In-state: {len(records)}, skipped: {skipped}", flush=True)
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
        idx[cat] = i + 1  # 0 = missing
        rev.append(cat)
    print(f"  Category index: {len(idx)} entries", flush=True)
    return idx, rev


# =========================================================================
# Record encoding (v3)
# =========================================================================


def encode_record_v3(rec: dict, prev_unified_id: int, cat_index: dict) -> bytes:
    buf = bytearray()

    # unified_id delta
    delta = rec["unified_id"] - prev_unified_id
    buf.extend(encode_varint(zigzag_encode(delta)))

    # centroid
    buf.extend(struct.pack("<i", coord_to_micro(rec["lon"])))
    buf.extend(struct.pack("<i", coord_to_micro(rec["lat"])))

    # name (required)
    buf.extend(encode_string_u16(rec["name"]))

    # category index (0 = missing)
    cat = rec.get("primary_category", "")
    buf.append(cat_index.get(cat, 0))

    # flags byte
    flags = 0
    phone = rec.get("phone", "")
    website = rec.get("website", "")
    address = rec.get("address", "")
    brand = rec.get("brand", "")
    status = rec.get("operating_status", "")
    email = rec.get("emails", "")
    social = rec.get("socials", "")

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

    # Check if we have extended attrs
    source_type = rec.get("source_type", SRC_OSM)
    source_id = rec.get("source_id", "")
    confidence = rec.get("confidence", 0)
    uid_alt = rec.get("unified_id_alt", 0)

    has_ext = source_type != SRC_OSM or source_id or confidence or uid_alt
    if has_ext:
        flags |= 0x80

    buf.append(flags)

    # Optional fields (same as v1)
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

    # Extended attrs (v3 only)
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

    # Fix up relative offsets in index
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

    # Write sidecar
    meta_path = output_path.replace(".ptiles", "_categories.json")
    with open(meta_path, "w") as f:
        json.dump(
            {
                "categories": cat_reverse,
                "total_places": len(records),
                "version": 3,
                "format": "unified-poi",
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

    ap = argparse.ArgumentParser(description="Build US POI ptiles from merged parquet")
    ap.add_argument(
        "--input",
        default="/home/aoi/code/rookery/geo/poi_lookup_tn.parquet",
        help="Input merged parquet path",
    )
    ap.add_argument(
        "--output-dir",
        default="/home/aoi/kino/projects/ptiles/data/states/",
        help="Output directory for .ptiles files",
    )
    ap.add_argument("--state", default="TN", help="Two-letter state abbreviation")
    ap.add_argument(
        "--full-us",
        action="store_true",
        help="Build all states from the same input (filters by bbox)",
    )
    ap.add_argument(
        "--no-dedup", action="store_true", help="Skip fuzzy dedup (for speed/testing)"
    )
    ap.add_argument(
        "--include-nationwide",
        action="store_true",
        help="Also write US.poi.ptiles (all records)",
    )
    args = ap.parse_args()

    t0 = time.time()

    print("Loading brand map...", flush=True)
    brand_map = load_brand_map()

    if args.full_us:
        states_to_build = list(STATE_BBOXES.keys())
    else:
        states_to_build = [args.state.upper()]

    for state in states_to_build:
        st_t0 = time.time()
        print(f"\n=== {state} ===", flush=True)

        bbox = STATE_BBOXES.get(state)
        records = load_parquet(args.input, bbox, brand_map)
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

        st_elapsed = time.time() - st_t0
        print(f"  {state} done in {st_elapsed:.1f}s", flush=True)

    # Nationwide file (all records, no bbox filter)
    if args.include_nationwide or args.full_us:
        st_t0 = time.time()
        print("\n=== US (nationwide) ===", flush=True)
        records = load_parquet(args.input, None, brand_map)
        if not args.no_dedup:
            records = fuzzy_dedup(records)
        cat_index, cat_reverse = build_category_index(records)
        blocks = group_and_encode(records, cat_index)
        os.makedirs(args.output_dir, exist_ok=True)
        out_path = os.path.join(args.output_dir, "US.business.ptiles")
        write_ptilesb(out_path, records, blocks, cat_reverse)
        print(f"  US done in {time.time() - st_t0:.1f}s", flush=True)

    elapsed = time.time() - t0
    print(f"\nTotal time: {elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
