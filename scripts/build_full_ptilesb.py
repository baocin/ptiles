#!/usr/bin/env python3
"""
Build all 51 state .business.ptiles files.
Standalone — no circular import issues.
"""

import sys
import os
import time
import hashlib
import struct
import json
import zstandard as zstd
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from encoding import coord_to_micro
from shared import write_header, HEADER_SIZE, write_index, train_dictionary
from states import get_state

sys.stdout.reconfigure(line_buffering=True)
import pyarrow.parquet as pq
import h3
from encoding import encode_varint, encode_string_u8, encode_string_u16, zigzag_encode

MAGIC = b"PTILESB\0"
VERSION = 4  # v4: no record_len, sequential IDs, i16 cell-relative coords
H3_RES = 7

SRC_FOURSQUARE = 2
SRC_OVERTURE = 1

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

STATE_DIR = "/mnt/core/poi_state_extracts"
OUT_DIR = "/home/aoi/kino/projects/ptiles/data/states/"


def make_unified_id(source, src_id):
    key = f"{source}:{src_id}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "little")


def load_brand_map():
    p = "/tmp/overture_brand_map.parquet"
    if not os.path.exists(p):
        return {}
    t = pq.read_table(p, columns=["overture_id", "brand_name"])
    return dict(
        zip(t.column("overture_id").to_pylist(), t.column("brand_name").to_pylist())
    )


def load_chain_index():
    p = "/mnt/core/poi_chain_index.parquet"
    if not os.path.exists(p):
        return {}
    t = pq.read_table(p, columns=["name", "count"])
    names = t.column("name").to_pylist()
    counts = t.column("count").to_pylist()
    return {
        names[i].lower(): int(counts[i])
        for i in range(len(names))
        if names[i] and counts[i]
    }


def load_state(st, brand_map, chain_idx):
    path = os.path.join(STATE_DIR, f"{st}.parquet")
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
    src, ids, names, lats, lons, cats = [
        t.column(c).to_pylist()
        for c in ["source", "source_id", "name", "lat", "lon", "primary_category"]
    ]
    addrs, cities, phones, websites, confs = [
        t.column(c).to_pylist()
        for c in ["address", "city", "phone", "website", "confidence"]
    ]

    # ponytail: the per-state extracts are cut with `WHERE state = '{ST}'` on the
    # vendor address column (Overture addresses[1].region, Foursquare region) and
    # that column is never cross-checked against lat/lon. ~0.2% of rows carry a
    # correct state string but garbage upstream coordinates — a Memphis hotel at
    # (59.59,-68.09) in Quebec, a Nashville shop in Siberia, London's Tobacco Dock
    # tagged city=Tiptonville — so they get H3-indexed into cells nowhere near the
    # state and pollute that state's file. Coordinates are what a .ptiles file is
    # indexed by, so a row we can't place is worse than a row we drop. Same padded
    # bbox guard build_parks/build_places/extract_all_states already apply.
    bbox = get_state(st)
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
        stype = SRC_FOURSQUARE if s == "foursquare" else SRC_OVERTURE
        sname = "foursquare" if s == "foursquare" else "overture"
        sid = ids[i]
        name = names[i] or ""
        brand = ""
        if stype == SRC_OVERTURE and brand_map and sid in brand_map:
            brand = brand_map[sid]
        chain_count = chain_idx.get(name.lower(), 0) if chain_idx else 0
        confidence = (
            round(confs[i] * 100) if confs[i] is not None and confs[i] > 0 else 0
        )

        records.append(
            {
                "source_type": stype,
                "source_id": sid,
                "lon": lons[i],
                "lat": lats[i],
                "name": name,
                "primary_category": cats[i] or "",
                "phone": phones[i] or "",
                "website": websites[i] or "",
                "address": addrs[i] or "",
                "city": cities[i] or "",
                "confidence": min(confidence, 100),
                "brand": brand,
                "chain_count": chain_count,
            }
        )
    if skipped_bbox:
        print(
            f"  Dropped {skipped_bbox:,} rows outside the {st} bbox (bad upstream coords)",
            flush=True,
        )
    return records


def encode_v4(rec, uid: int, cat_idx: dict, cell_center_micro: tuple) -> bytes:
    """Encode a single v4 business record — no record_len, sequential uid, i16 coords."""
    buf = bytearray()

    # Sequential uid (zigzag varint from 0-based counter)
    buf.extend(encode_varint(zigzag_encode(uid)))

    # Cell-relative i16 coords
    lon_micro = coord_to_micro(rec["lon"])
    lat_micro = coord_to_micro(rec["lat"])
    offset_lon = lon_micro - cell_center_micro[0]
    offset_lat = lat_micro - cell_center_micro[1]
    buf.extend(struct.pack("<hh", offset_lon, offset_lat))

    buf.extend(encode_string_u16(rec["name"]))
    buf.append(cat_idx.get(rec["primary_category"], 0))

    flags = 0
    if rec["phone"]:
        flags |= 0x01
    if rec["website"]:
        flags |= 0x02
    if rec["address"]:
        flags |= 0x04
    if rec["brand"]:
        flags |= 0x08
    if rec["chain_count"] > 0:
        flags |= 0x80
    buf.append(flags)

    if rec["phone"]:
        buf.extend(encode_string_u8(rec["phone"]))
    if rec["website"]:
        buf.extend(encode_string_u8(rec["website"]))
    if rec["address"]:
        buf.extend(encode_string_u16(rec["address"]))
    if rec["brand"]:
        buf.extend(encode_string_u8(rec["brand"]))
    if rec["chain_count"] > 0:
        buf.append(min(rec["chain_count"], 255))

    # Extended attrs (v4: updated bits for star_rating, opening_hours, etc.)
    ext_flags = 0
    if rec["source_type"] != 0:
        ext_flags |= 0x01
    if rec["source_id"]:
        ext_flags |= 0x02
    if rec["confidence"]:
        ext_flags |= 0x04
    # 0x08 (unified_id_alt) dropped in v4 — no hash-based IDs
    if ext_flags:
        buf.extend(struct.pack("<H", ext_flags))
        if ext_flags & 0x01:
            buf.append(rec["source_type"])
        if ext_flags & 0x02:
            buf.extend(encode_string_u16(rec["source_id"]))
        if ext_flags & 0x04:
            buf.append(min(rec["confidence"], 255))

    # v4: no record_len prefix — records are parsed sequentially by feature_count
    return bytes(buf)


def build_state_ptiles(state, brand_map, chain_idx):
    st_t0 = time.time()
    print(f"\n=== {state} ===", flush=True)
    records = load_state(state, brand_map, chain_idx)
    if not records:
        print(f"  No records for {state}", flush=True)
        return
    print(f"  {len(records):,} records", flush=True)

    # Category index
    cat_counts = defaultdict(int)
    for r in records:
        if r["primary_category"]:
            cat_counts[r["primary_category"]] += 1
    sorted_cats = sorted(cat_counts.items(), key=lambda x: -x[1])
    cat_idx = {c: i + 1 for i, (c, _) in enumerate(sorted_cats[:254])}
    cat_rev = [c for c, _ in sorted_cats[:254]]
    print(f"  Categories: {len(cat_idx)}", flush=True)

    # Group by H3
    cells = defaultdict(list)
    for r in records:
        c = int(h3.latlng_to_cell(r["lat"], r["lon"], H3_RES), 16)
        cells[c].append(r)
    print(f"  H3 cells: {len(cells)}", flush=True)

    # Encode blocks (v4: sequential uid, cell-relative i16 coords, no record_len)
    blocks = {}
    uid_counter = 0
    for cell in sorted(cells):
        recs = cells[
            cell
        ]  # no sort needed — sequential uids are assigned in iteration order
        # Get cell center for i16 relative encoding
        cell_hex = format(cell, "x")
        clat, clon = h3.cell_to_latlng(cell_hex)
        cell_center_micro = (coord_to_micro(clon), coord_to_micro(clat))

        buf = bytearray()
        for r in recs:
            buf.extend(encode_v4(r, uid_counter, cat_idx, cell_center_micro))
            uid_counter += 1
        blocks[cell] = bytes(buf)

    # Train dict
    samples = list(blocks.values())[:1000]
    print(
        f"  Training dict on {len(samples)} samples ({sum(len(s) / 1024 for s in samples):.0f} KB)...",
        flush=True,
    )
    dict_data = train_dictionary(samples)
    print(f"  Dict: {len(dict_data)} bytes", flush=True)

    # Compress
    compressed = {}
    for cell, raw in blocks.items():
        d = zstd.ZstdCompressionDict(dict_data)
        compressed[cell] = zstd.ZstdCompressor(level=12, dict_data=d).compress(raw)

    lats = [r["lat"] for r in records]
    lons = [r["lon"] for r in records]

    # Index
    sorted_cells = sorted(compressed.keys())
    index_entries = []
    roff = 0
    for cell in sorted_cells:
        cb = compressed[cell]
        index_entries.append(
            {
                "h3_cell": cell,
                "block_offset": roff,
                "block_length": len(cb),
                "feature_count": len(cells.get(cell, [])),
            }
        )
        roff += len(cb)

    lats = [r["lat"] for r in records]
    lons = [r["lon"] for r in records]

    # Write
    out = os.path.join(OUT_DIR, f"{state}.business.ptiles")
    hdr = HEADER_SIZE
    do, dl = hdr, len(dict_data)
    io, il = do + dl, 4 + len(index_entries) * 19
    bo = io + il

    with open(out, "wb") as f:
        write_header(
            f,
            MAGIC,
            VERSION,
            min(lats),
            min(lons),
            max(lats),
            max(lons),
            len(records),
            len(compressed),
            do,
            dl,
            io,
            il,
            bo,
        )
        f.write(dict_data)
        write_index(f, index_entries)
        for e in index_entries:
            f.write(compressed[e["h3_cell"]])

    # Fix offsets
    idx_pos = io + 4
    with open(out, "r+b") as f:
        for e in index_entries:
            f.seek(idx_pos + 8)
            f.write((bo + e["block_offset"]).to_bytes(6, "little"))
            idx_pos += 19

    size = os.path.getsize(out)
    print(f"  Written: {size / 1024 / 1024:.1f} MB ({len(records):,} POIs)", flush=True)
    print(f"  Time: {time.time() - st_t0:.1f}s", flush=True)

    # Categories sidecar
    meta = out.replace(".ptiles", "_categories.json")
    with open(meta, "w") as f:
        json.dump(
            {
                "categories": cat_rev,
                "total_places": len(records),
                "version": 4,
                "format": "unified-poi",
            },
            f,
            indent=2,
        )


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()
    print("Loading brand map...", flush=True)
    brand_map = load_brand_map()
    print(f"  {len(brand_map):,} brands", flush=True)
    print("Loading chain index...", flush=True)
    chain_idx = load_chain_index()
    print(f"  {len(chain_idx):,} entries", flush=True)

    for st in STATES:
        build_state_ptiles(st, brand_map, chain_idx)

    print(f"\nTotal: {time.time() - t0:.0f}s for all 51 states", flush=True)


if __name__ == "__main__":
    main()
