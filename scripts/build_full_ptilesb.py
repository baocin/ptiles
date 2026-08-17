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
from boundaries import stamp_boundary
from states import get_state
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from ptiles.categories import GROUPS, canonical, group_of
from ptiles.dedupe import dedupe
from ptiles.flightnodes import flight_categories, is_flight_node
from encoding import coord_to_micro
from shared import write_header, HEADER_SIZE, write_index, train_dictionary

sys.stdout.reconfigure(line_buffering=True)
import pyarrow.parquet as pq
import h3
from encoding import encode_varint, encode_string_u8, encode_string_u16, zigzag_encode

MAGIC = b"PTILESB\0"
VERSION = 5  # v5 adds name:en (0x10) and carries brand in-record  # v4: no record_len, sequential IDs, i16 cell-relative coords
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
    # Resolved from STATE_DIR at call time, so redirecting STATE_DIR moves this
    # too. It used to be hardcoded to /tmp/overture_brand_map.parquet, which is
    # gone -- and a missing file returns {} rather than failing, so the build kept
    # succeeding while silently dropping every brand name.
    p = os.path.join(STATE_DIR, "brand_map.parquet")
    if not os.path.exists(p):
        print(f"  WARNING: no brand map at {p} -- brands will be empty", flush=True)
        return {}
    t = pq.read_table(p, columns=["overture_id", "brand_name"])
    return dict(
        zip(t.column("overture_id").to_pylist(), t.column("brand_name").to_pylist())
    )


def load_chain_index():
    # Same story as the brand map: was /mnt/core/poi_chain_index.parquet, gone,
    # and absence degraded to every chain_count being 0 without a word.
    p = os.path.join(STATE_DIR, "chain_index.parquet")
    if not os.path.exists(p):
        print(f"  WARNING: no chain index at {p} -- chain counts will be 0", flush=True)
        return {}
    t = pq.read_table(p, columns=["name", "count"])
    names = t.column("name").to_pylist()
    counts = t.column("count").to_pylist()
    return {
        names[i].lower(): int(counts[i])
        for i in range(len(names))
        if names[i] and counts[i]
    }



_BASE_COLUMNS = [
    "source", "source_id", "name", "lat", "lon", "primary_category",
    "address", "city", "phone", "website", "confidence",
]
# v5 additions. Named only when the extract actually has them: an extract built
# before build_poi_extracts carried them through its projection has neither, and
# naming a missing column is an error rather than a NULL.
_OPTIONAL_COLUMNS = ["brand_name", "name_en"]


def _columns_for(path):
    have = set(pq.read_schema(path).names)
    return _BASE_COLUMNS + [c for c in _OPTIONAL_COLUMNS if c in have]


def load_state(st, brand_map, chain_idx):
    path = os.path.join(STATE_DIR, f"{st}.parquet")
    t = pq.read_table(
        path,
        columns=_columns_for(path),
    )
    src, ids, names, lats, lons, cats = [
        t.column(c).to_pylist()
        for c in ["source", "source_id", "name", "lat", "lon", "primary_category"]
    ]
    addrs, cities, phones, websites, confs = [
        t.column(c).to_pylist()
        for c in ["address", "city", "phone", "website", "confidence"]
    ]
    # Optional: an extract produced before the projection carried these has
    # neither column, and the record falls back to the sidecar brand map.
    have = set(t.column_names)
    brands_col = t.column("brand_name").to_pylist() if "brand_name" in have else None
    name_en_col = t.column("name_en").to_pylist() if "name_en" in have else None

    records = []
    for i in range(len(t)):
        s = src[i]
        stype = SRC_FOURSQUARE if s == "foursquare" else SRC_OVERTURE
        sname = "foursquare" if s == "foursquare" else "overture"
        sid = ids[i]
        name = names[i] or ""
        # The extract now carries brand_name and name_en directly. The sidecar
        # brand map stays as a fallback: it is how brands reached v4 at all, and
        # an older extract will not have the column.
        brand = (brands_col[i] or "") if brands_col is not None else ""
        if not brand and stype == SRC_OVERTURE and brand_map and sid in brand_map:
            brand = brand_map[sid]
        name_en = (name_en_col[i] or "") if name_en_col is not None else ""
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
                "name_en": name_en,
                "chain_count": chain_count,
            }
        )
    return _without_flights(st, records)


def _without_flights(st, records):
    """Drop the flights, the gates, and the category that holds them.

    A departure board is not a set of places: the source carries one around
    every airport, and none of it is somewhere a person can be routed to.

    The category is the real filter and the names are how it is found -- in
    Tennessee the flight category holds 1,710 records of which only 922 are
    named recognisably, the rest being `Im On A Plane`, `Seat 3C In First
    Class`, `First Class`. See `ptiles/flightnodes.py`. The client applies the
    name half at read time so packs already downloaded improve too, but it
    cannot do this half: a pack carries a category *index*, numbered per state,
    and never the label.
    """
    categories = flight_categories(records)
    kept = []
    by_category = by_name = 0
    for rec in records:
        if rec["primary_category"] in categories:
            by_category += 1
        elif is_flight_node(rec["name"]):
            by_name += 1
        else:
            kept.append(rec)
    if by_category or by_name:
        print(
            f"  {st}: dropped {by_category + by_name} flight records "
            f"({by_category} by category, {by_name} by name); "
            f"flight categories: {sorted(categories) or 'none'}",
            flush=True,
        )
    # One place, one record. Foursquare and Overture were merged without a
    # dedupe pass, so most real places are in here twice under slightly
    # different spellings: `Mt Juliet Family Vision` six times, `FirstBank` and
    # `Firstbank`, `B & E Automotive` and `B&E Automotive`. Over Tennessee this
    # collapses 137,120 of 829,528 records -- 16.5% -- and the survivor is
    # filled in from the ones it absorbs, so no phone or website is lost with
    # the row. See `ptiles/dedupe.py`.
    kept, absorbed = dedupe(kept)
    if absorbed:
        print(
            f"  {st}: merged {absorbed} duplicate records "
            f"({absorbed / (len(kept) + absorbed) * 100:.1f}%), {len(kept)} remain",
            flush=True,
        )
    return kept


def category_byte(label: str, cat_idx: dict) -> int:
    """The byte a record stores for its category.

    0 only when the source gave none. A label that exists but ranked below the
    254 the field can hold becomes [`CATEGORY_OTHER`], so a truncated tail is
    visible as itself rather than as missing data.
    """
    if not label:
        return 0
    return cat_idx.get(label, CATEGORY_OTHER)


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
    buf.append(category_byte(rec["primary_category"], cat_idx))

    flags = 0
    if rec["phone"]:
        flags |= 0x01
    if rec["website"]:
        flags |= 0x02
    if rec["address"]:
        flags |= 0x04
    if rec["brand"]:
        flags |= 0x08
    if rec.get("name_en"):
        # v5. Free in the v4 decoder: v1/v2 spent 0x10 on operating_status,
        # which v4 dropped and decode_business_record_v4 never reads.
        flags |= 0x10
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
    if rec.get("name_en"):
        # After brand and before chain_count, matching the flag order: these
        # records carry no length prefix, so field order is the contract.
        buf.extend(encode_string_u16(rec["name_en"]))
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



# Aux section magic: the category table a pack carries about itself.
CATEGORY_AUX_MAGIC = b"PTCT"
# The byte a record carries when its category is known but did not fit.
#
# Only 254 categories fit in the field, so the rest of the tail used to be
# written as 0 -- the same value as "this record has no category at all". That
# is not truncation, it is truncation pretending to be absence: 37% of
# Tennessee's records read as uncategorised and nobody could say how many of
# them actually had one. 255 says "categorised, below the cut", and 0 goes back
# to meaning what it says.
CATEGORY_OTHER = 255
# Byte offset of aux_offset in the 256-byte header; aux_length follows it.
AUX_OFFSET_FIELD = 72
CATEGORY_AUX_VERSION = 1


def build_id(state: str, labels: list[str], record_count: int) -> str:
    """A short stamp identifying this build of this state.

    Derived from what the build produced rather than from the clock, so two
    runs over the same input agree and a reader can tell whether a sidecar
    belongs to a pack. The category *numbering* is what drifts -- it is a
    frequency rank within one state's build -- so the labels in rank order are
    exactly the thing worth hashing.
    """
    import hashlib

    digest = hashlib.sha256()
    digest.update(state.encode())
    digest.update(str(record_count).encode())
    for label in labels:
        digest.update(b"\x00")
        digest.update(label.encode())
    return digest.hexdigest()[:12]


def category_aux(state: str, cat_idx: dict, record_count: int) -> bytes:
    """The category table, to be carried inside the pack.

    A pack that names its own categories cannot drift from a sidecar, because
    there is nothing to pair it with. Measured on the published Tennessee file
    this is about 6 KB against 54 MB, and it is what lets a client show
    "Elementary School" instead of `business:94`, or filter by category at
    all -- today it can read the number and nothing else.

    Layout, little-endian:

        magic   4  b"PTCT"
        version 1
        build   1 + n   short stamp, see `build_id`
        count   2       number of entries
        entry   1 index, 1 group, 1 label length, n label bytes
    """
    ordered = sorted(cat_idx.items(), key=lambda kv: kv[1])
    labels = [label for label, _ in ordered]
    # 255 is a category like any other as far as a reader is concerned, and it
    # has to be named or the byte resolves to nothing.
    ordered = ordered + [("other", CATEGORY_OTHER)]
    stamp = build_id(state, labels, record_count).encode()

    out = bytearray(CATEGORY_AUX_MAGIC)
    out.append(CATEGORY_AUX_VERSION)
    out.append(len(stamp))
    out.extend(stamp)
    out.extend(len(ordered).to_bytes(2, "little"))
    for label, index in ordered:
        leaf = canonical(label).encode("utf-8")[:255]
        group = GROUPS.index(group_of(label))
        out.append(index)
        out.append(group)
        out.append(len(leaf))
        out.extend(leaf)
    return bytes(out)


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
    truncated = sum(
        1 for r in records if r["primary_category"] and r["primary_category"] not in cat_idx
    )
    if truncated:
        distinct = len({
            r["primary_category"]
            for r in records
            if r["primary_category"] and r["primary_category"] not in cat_idx
        })
        print(
            f"  {distinct} categories past the 254 the field holds: "
            f"{truncated} records written as 'other' rather than as uncategorised",
            flush=True,
        )

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
    out = os.path.join(OUT_DIR, f"{state}.business_v{VERSION}.ptiles")
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
        # The category table goes last, so the offsets above are unaffected and
        # a reader that ignores aux reads exactly the file it read before.
        aux_offset = f.tell()
        aux = category_aux(state, cat_idx, len(records))
        f.write(aux)

    # Fix offsets
    idx_pos = io + 4
    with open(out, "r+b") as f:
        for e in index_entries:
            f.seek(idx_pos + 8)
            f.write((bo + e["block_offset"]).to_bytes(6, "little"))
            idx_pos += 19
        # aux_offset (u64) and aux_length (u32) sit at bytes 72 and 80 of the
        # 256-byte header -- taken from the reader that has to agree,
        # core/src/header.rs, not from counting the struct by eye. They are
        # only known once the table has been written.
        f.seek(AUX_OFFSET_FIELD)
        f.write(aux_offset.to_bytes(8, "little"))
        f.write(len(aux).to_bytes(4, "little"))

    # Boundary last: it goes on the end of the file, after the offset fix-up
    # above has finished rewriting the index in place.
    _region = get_state(state)
    if _region is not None:
        stamp_boundary(out, _region)

    size = os.path.getsize(out)
    print(f"  Written: {size / 1024 / 1024:.1f} MB ({len(records):,} POIs)", flush=True)
    print(f"  Time: {time.time() - st_t0:.1f}s", flush=True)

    # Categories sidecar. Named off the state, not off `out`: the published name
    # is {ST}.business_categories.json, with no version in it, so deriving it
    # from the versioned .ptiles name would rename the sidecar every version bump.
    meta = os.path.join(OUT_DIR, f"{state}.business_categories.json")
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
