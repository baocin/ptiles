#!/usr/bin/env python3
"""
Build rail.ptiles from per-state OSM PBF files.

Uses osmium FileProcessor + KeyFilter for fast railway extraction.
Format: PTILEST magic, v1, v2 merged-block.

Captures:
  - Railway ways (tracks): rail, tram, light_rail, subway, monorail, narrow_gauge, funicular, preserved
  - Station nodes: station, halt, tram_stop, subway_entrance
"""

import sys
import struct
import time

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

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
MAGIC = b"PTILEST\x00"
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

# Track types we capture (as ways with linestring geometry)
TRACK_TYPES = {
    "rail",
    "tram",
    "light_rail",
    "subway",
    "monorail",
    "narrow_gauge",
    "funicular",
    "preserved",
}
# Station types we capture (as nodes with point geometry)
STATION_TYPES = {"station", "halt", "tram_stop", "subway_entrance"}

RT = [
    "rail",
    "subway",
    "light_rail",
    "tram",
    "monorail",
    "narrow_gauge",
    "funicular",
    "station",
    "halt",
    "tram_stop",
    "subway_entrance",
]
RTI = {t: i for i, t in enumerate(RT)}


def extract(pbf):
    import osmium

    fp = osmium.FileProcessor(pbf).with_filter(osmium.filter.KeyFilter("railway"))
    features = []
    for obj in fp:
        is_node = hasattr(obj, "lat")
        rail_type = None
        name = None
        for t in obj.tags:
            if t.k == "railway" and t.v:
                if t.v in TRACK_TYPES or t.v in STATION_TYPES:
                    rail_type = t.v
            elif t.k == "name":
                name = t.v
        if not rail_type:
            continue

        geom_type = 0  # 0=linestring, 1=point
        if rail_type in STATION_TYPES and is_node:
            geom_type = 1

        if geom_type == 0:
            # Linestring: extract way nodes
            if not hasattr(obj, "nodes"):
                continue
            coords = []
            try:
                for n in obj.nodes:
                    if n.location.valid():
                        coords.append((n.lon, n.lat))
            except Exception:
                continue
            if len(coords) < 2:
                continue
            key_coord = coords[0]
        else:
            # Point: station node
            key_coord = (obj.lon, obj.lat)

        try:
            cell = h3.latlng_to_cell(key_coord[1], key_coord[0], H3_RES)
        except Exception:
            continue

        features.append(
            {
                "osm_id": obj.id,
                "rail_type": rail_type,
                "geom_type": geom_type,
                "coords": coords if geom_type == 0 else [(obj.lon, obj.lat)],
                "name": name,
                "cell": int(cell, 16) if isinstance(cell, str) else cell,
            }
        )

    return features


def encode_coordinates(coords):
    """Encode coordinate sequence as varint deltas."""
    if not coords:
        return b""
    buf = bytearray()
    first_lon = round(coords[0][0] * 100_000)
    first_lat = round(coords[0][1] * 100_000)
    prev_lon = first_lon
    prev_lat = first_lat
    for lon, lat in coords[1:]:
        cur_lon = round(lon * 100_000)
        cur_lat = round(lat * 100_000)
        buf.extend(encode_varint(zigzag_encode(cur_lon - prev_lon)))
        buf.extend(encode_varint(zigzag_encode(cur_lat - prev_lat)))
        prev_lon, prev_lat = cur_lon, cur_lat
    return bytes(buf)


def enc(feat, pid):
    buf = bytearray()
    buf.extend(encode_varint(zigzag_encode(feat["osm_id"] - pid)))
    buf.append(feat["geom_type"])  # 0=linestring, 1=point
    if feat["geom_type"] == 0:
        # Linestring: vertex count + first absolute + deltas
        coords = feat.get("coords", [])
        if len(coords) < 2:
            return b""
        n_verts = len(coords)
        buf.extend(struct.pack("<H", n_verts))
        buf.extend(
            struct.pack(
                "<ii", round(coords[0][0] * 100000), round(coords[0][1] * 100000)
            )
        )
        buf.extend(encode_coordinates(coords))
    else:
        # Point: lon, lat
        pt = feat.get("coords", [(0, 0)])[0]
        buf.extend(struct.pack("<ii", round(pt[0] * 100000), round(pt[1] * 100000)))
    # Rail type index
    buf.append(RTI.get(feat["rail_type"], 0))
    # Flags byte
    flags = 0
    if feat.get("name"):
        flags |= 0x01
    buf.append(flags)
    if feat.get("name"):
        nb = feat["name"].encode("utf-8")
        buf.extend(struct.pack("<H", len(nb)))
        buf.extend(nb)
    return bytes(buf)


def build_state(abbr):
    pbfn = PBF_MAP.get(abbr)
    if not pbfn:
        return {"abbr": abbr, "error": "no mapping"}
    pbfp = PBF_DIR / f"{pbfn}-latest.osm.pbf"
    if not pbfp.exists():
        return {"abbr": abbr, "error": "no pbf"}
    s = get_state(abbr)
    t0 = time.time()

    features = extract(str(pbfp))
    if not features:
        return {"abbr": abbr, "features": 0, "time_s": round(time.time() - t0, 1)}

    pc = defaultdict(list)
    for f in features:
        cell_hex = hex(f["cell"])[2:] if isinstance(f["cell"], int) else str(f["cell"])
        pc[cell_hex].append(f)
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
            for f in pc[cell]:
                rs.append(enc(f, pid))
                pid = f["osm_id"]
            cr.append((int(cell, 16), rs))
            pd.append((int(cell, 16), len(rs)))
        if not cr:
            continue
        cha = hex(int(bch[0], 16))[2:]
        cla, clo = h3.cell_to_latlng(cha)
        if not isinstance(clo, (int, float)) or not isinstance(cla, (int, float)):
            continue
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
                    "min_lon": round(
                        min(f["coords"][0][0] for f in pc[hex(cell)[2:]]) * 100000
                    )
                    if pc.get(hex(cell)[2:])
                    else 0,
                    "min_lat": round(
                        min(f["coords"][0][1] for f in pc[hex(cell)[2:]]) * 100000
                    )
                    if pc.get(hex(cell)[2:])
                    else 0,
                    "max_lon": round(
                        max(f["coords"][0][0] for f in pc[hex(cell)[2:]]) * 100000
                    )
                    if pc.get(hex(cell)[2:])
                    else 0,
                    "max_lat": round(
                        max(f["coords"][0][1] for f in pc[hex(cell)[2:]]) * 100000
                    )
                    if pc.get(hex(cell)[2:])
                    else 0,
                }
            )
            off += 1

    if not mb:
        return {"abbr": abbr, "features": 0}

    # Skip dictionary training for sparse rail data; compress without dict
    dd = b""
    cbs = [zstd.ZstdCompressor(level=1).compress(b) for b in mb]

    tf = sum(e["feature_count"] for e in pi)
    do = HEADER_SIZE
    dl = len(dd)
    io = do + dl
    il = 4 + len(pi) * INDEX_ENTRY_SIZE_V2
    bo = io + il

    ie = []
    for idx, pc_ in enumerate(pi):
        block_idx = idx // bs
        ie.append(
            {
                **pc_,
                "block_offset": bo + sum(len(cb) for cb in cbs[:block_idx]),
                "block_length": len(cbs[block_idx]) if block_idx < len(cbs) else 0,
            }
        )

    op = OUTPUT_DIR / f"{abbr}.rail.ptiles"
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

    sz = op.stat().st_size
    return {
        "abbr": abbr,
        "features": tf,
        "cells": len(pc),
        "bytes": sz,
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
        p.print_help()
        return
    for abbr in targets:
        try:
            r = build_state(abbr)
            if r and r.get("features"):
                print(
                    f"  {r['abbr']:2s} {r['features']:6d} features  {r['bytes']:10,d} B  {r['time_s']:6.1f}s",
                    flush=True,
                )
            elif r:
                print(
                    f"  {r['abbr']:2s}  {r.get('features', 0)}  ({r.get('error', '')})",
                    flush=True,
                )
        except Exception as e:
            print(f"  ERROR {abbr}: {e}", flush=True)
            import traceback

            traceback.print_exc()


if __name__ == "__main__":
    main()
