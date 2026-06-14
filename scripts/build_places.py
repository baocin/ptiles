#!/usr/bin/env python3
"""
Build places.ptiles from per-state OSM PBF files.
Uses osmium FileProcessor + KeyFilter for fast node extraction.
Format: PTILESP v1, v2 merged-block.
"""

import sys

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

import struct
import time
from pathlib import Path
from collections import defaultdict

import h3
import zstandard as zstd

from shared import (
    encode_varint,
    zigzag_encode,
    encode_index_entry_v2,
    INDEX_ENTRY_SIZE_V2,
    encode_merged_block,
    write_header,
    HEADER_SIZE,
)
from states import STATES, get_state

OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
PBF_DIR = Path("/home/aoi/kino/projects/ptiles/data/pbfs")
MAGIC = b"PTILESP\x00"
VERSION = 1
H3_RES = 7

PBF_MAP = {
    "AL": "alabama",
    "AK": "alaska",
    "AZ": "arizona",
    "AR": "arkansas",
    "CA": "california",
    "CO": "colorado",
    "CT": "connecticut",
    "DE": "delaware",
    "DC": "district-of-columbia",
    "FL": "florida",
    "GA": "georgia",
    "HI": "hawaii",
    "ID": "idaho",
    "IL": "illinois",
    "IN": "indiana",
    "IA": "iowa",
    "KS": "kansas",
    "KY": "kentucky",
    "LA": "louisiana",
    "ME": "maine",
    "MD": "maryland",
    "MA": "massachusetts",
    "MI": "michigan",
    "MN": "minnesota",
    "MS": "mississippi",
    "MO": "missouri",
    "MT": "montana",
    "NE": "nebraska",
    "NV": "nevada",
    "NH": "new-hampshire",
    "NJ": "new-jersey",
    "NM": "new-mexico",
    "NY": "new-york",
    "NC": "north-carolina",
    "ND": "north-dakota",
    "OH": "ohio",
    "OK": "oklahoma",
    "OR": "oregon",
    "PA": "pennsylvania",
    "RI": "rhode-island",
    "SC": "south-carolina",
    "SD": "south-dakota",
    "TN": "tennessee",
    "TX": "texas",
    "UT": "utah",
    "VT": "vermont",
    "VA": "virginia",
    "WA": "washington",
    "WV": "west-virginia",
    "WI": "wisconsin",
    "WY": "wyoming",
}

PT = [
    "city",
    "town",
    "village",
    "hamlet",
    "neighborhood",
    "suburb",
    "borough",
    "quarter",
    "isolated_dwelling",
]
PTI = {t: i for i, t in enumerate(PT)}


def extract(pbf):
    import osmium

    fp = osmium.FileProcessor(pbf).with_filter(osmium.filter.KeyFilter("place"))
    out = []
    for obj in fp:
        lon = getattr(obj, "lon", None)
        if lon is None:
            continue
        pt = nm = None
        pop = 0
        an = None
        al = None
        for t in obj.tags:
            if t.k == "place" and t.v:
                pt = t.v
            elif t.k == "name" and t.v:
                nm = t.v
            elif t.k == "population" and t.v:
                try:
                    pop = int(t.v)
                except:
                    pass
            elif t.k == "alt_name" and t.v:
                an = t.v
            elif t.k == "admin_level" and t.v:
                try:
                    al = int(t.v)
                except:
                    pass
        if not pt or not nm:
            continue
        try:
            c = h3.latlng_to_cell(obj.lat, lon, 7)
        except:
            continue
        out.append(
            {
                "osm_id": obj.id,
                "lon": lon,
                "lat": obj.lat,
                "place_type": pt,
                "population": pop,
                "name": nm,
                "alt_name": an,
                "admin_level": al,
                "cell": int(c, 16) if isinstance(c, str) else c,
            }
        )
    return out


def enc(p, pid):
    b = bytearray()
    b.extend(encode_varint(zigzag_encode(p["osm_id"] - pid)))
    b.extend(struct.pack("<ii", round(p["lon"] * 100000), round(p["lat"] * 100000)))
    b.append(PTI.get(p["place_type"], 4))
    b.extend(encode_varint(p["population"]))
    nb = (p["name"] or "").encode("utf-8")
    b.extend(struct.pack("<H", len(nb)))
    b.extend(nb)
    f = 0
    if p.get("alt_name"):
        f |= 1
    if p.get("admin_level") is not None:
        f |= 2
    b.append(f)
    if p.get("alt_name"):
        an = p["alt_name"].encode("utf-8")
        b.extend(struct.pack("<H", len(an)))
        b.extend(an)
    if p.get("admin_level") is not None:
        b.append(p["admin_level"])
    return bytes(b)


def build_state(abbr):
    pbfn = PBF_MAP.get(abbr)
    if not pbfn:
        return {"abbr": abbr, "error": "no mapping"}
    pbfp = PBF_DIR / f"{pbfn}-latest.osm.pbf"
    if not pbfp.exists():
        return {"abbr": abbr, "error": "no pbf"}
    s = get_state(abbr)
    t0 = time.time()
    places = extract(str(pbfp))
    if not places:
        return {"abbr": abbr, "places": 0, "time_s": round(time.time() - t0, 1)}
    pc = defaultdict(list)
    for p in places:
        pc[p["cell"]].append(p)
    for c in pc:
        pc[c].sort(key=lambda p: p["osm_id"])
    sc = sorted(pc.keys())
    mb = []
    pi = []
    bs = 8
    for i in range(0, len(sc), bs):
        bch = sc[i : i + bs]
        cr = []
        pd = []
        for cell in bch:
            rs = []
            pid = 0
            for p in pc[cell]:
                rs.append(enc(p, pid))
                pid = p["osm_id"]
            cr.append((cell, rs))
            pd.append((cell, len(rs)))
        ch = hex(bch[0])[2:]
        cla, clo = h3.cell_to_latlng(ch)
        blk = encode_merged_block(cr, round(clo * 100000), round(cla * 100000))
        mb.append(blk)
        off = 0
        for cell, cnt in pd:
            pi.append(
                {
                    "h3_cell": cell,
                    "block_offset": 0,
                    "block_length": len(blk),
                    "feature_count": cnt,
                    "cell_index": off,
                    "min_lon": round(min(p["lon"] for p in pc[cell]) * 100000),
                    "min_lat": round(min(p["lat"] for p in pc[cell]) * 100000),
                    "max_lon": round(max(p["lon"] for p in pc[cell]) * 100000),
                    "max_lat": round(max(p["lat"] for p in pc[cell]) * 100000),
                }
            )
            off += 1
    dd = zstd.train_dictionary(512 * 1024, mb[:2000]).as_bytes()
    zd = zstd.ZstdCompressionDict(dd)
    cbs = [zstd.ZstdCompressor(level=12, dict_data=zd).compress(b) for b in mb]
    # Debug: check first compressed block magic
    if cbs:
        print(
            f"  First block: raw={len(mb[0])}B compressed={len(cbs[0])}B magic={cbs[0][:4].hex()}",
            flush=True,
        )
    tf = sum(e["feature_count"] for e in pi)
    do = HEADER_SIZE
    dl = len(dd)
    io = do + dl
    il = 4 + len(pi) * INDEX_ENTRY_SIZE_V2
    bo = io + il
    cur = bo
    ie = []
    # Map each per-cell index entry to its compressed block
    for idx, pc_ in enumerate(pi):
        block_idx = idx // bs
        ie.append(
            {
                **pc_,
                "block_offset": cur + sum(len(cb) for cb in cbs[:block_idx]),
                "block_length": len(cbs[block_idx]) if block_idx < len(cbs) else 0,
            }
        )
    # The actual blocks_offset should match where data starts in file
    # Recompute: blocks start after header + dict + actual index bytes
    actual_il = 4 + len(pi) * INDEX_ENTRY_SIZE_V2
    bo = io + actual_il
    # Recalculate block offsets from new bo
    cur = bo
    for idx, e in enumerate(ie):
        block_idx = idx // bs
        e["block_offset"] = cur + sum(len(cb) for cb in cbs[:block_idx])
    ie.sort(key=lambda e: e["h3_cell"])
    op = OUTPUT_DIR / f"{abbr}.places.ptiles"
    with open(op, "wb") as f:
        write_header(
            f,
            MAGIC,
            VERSION,
            s.min_lat,
            s.min_lon,
            s.max_lat,
            s.max_lon,
            tf,
            len(mb),
            do,
            dl,
            io,
            il,
            bo,
        )
        f.write(dd)
        f.write(struct.pack("<I", len(ie)))
        for e in ie:
            f.write(
                encode_index_entry_v2(
                    e["h3_cell"],
                    e["min_lon"],
                    e["min_lat"],
                    e["max_lon"],
                    e["max_lat"],
                    e["block_offset"],
                    e["block_length"],
                    e["feature_count"],
                    e["cell_index"],
                )
            )
        for cb in cbs:
            f.write(cb)
    return {
        "abbr": abbr,
        "places": tf,
        "cells": len(pc),
        "bytes": op.stat().st_size,
        "time_s": round(time.time() - t0, 1),
    }


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true")
    p.add_argument("--states")
    args = p.parse_args()
    targets = []
    if args.all:
        targets = [s.abbr for s in STATES]
    elif args.states:
        for a in args.states.split(","):
            s = get_state(a.strip())
            if s:
                targets.append(s.abbr)
            else:
                print(f"Unknown: {a}")
    else:
        p.print_help()
        return
    for abbr in targets:
        try:
            r = build_state(abbr)
            if r and r.get("places"):
                print(
                    f"  {r['abbr']:2s} {r['places']:6d} places  {r['cells']:4d} cells  {r['bytes']:10,d} B  {r['time_s']:6.1f}s",
                    flush=True,
                )
            elif r:
                print(f"  {r['abbr']:2s}  0 places  ({r.get('error', '')})", flush=True)
        except Exception as e:
            print(f"  ERROR {abbr}: {e}", flush=True)
            import traceback

            traceback.print_exc()
    print("\nDone")


if __name__ == "__main__":
    main()
