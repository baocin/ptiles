#!/usr/bin/env python3
"""
Build parks.ptiles from per-state OSM PBF files.

Format: PTILESN magic, v1, v2 merged-block.

Captures polygon features tagged as:
  leisure=park, leisure=golf_course, leisure=nature_reserve,
  leisure=recreation_ground, leisure=playground,
  boundary=national_park, boundary=protected_area
"""

import sys
import struct
import time

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

from pathlib import Path
from collections import defaultdict
import osmium
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
MAGIC = b"PTILESN\x00"
VERSION = 1
H3_RES = 7

PARK_TAGS = {
    "leisure": {
        "park",
        "golf_course",
        "nature_reserve",
        "recreation_ground",
        "playground",
    },
    "boundary": {"national_park", "protected_area"},
}

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


class ParkHandler(osmium.SimpleHandler):
    """Extract park polygons from ways and multipolygon relations."""

    def __init__(self):
        super().__init__()
        self.parks = []

    def way(self, w):
        pt = self._park_type(w.tags)
        if not pt:
            return
        coords = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
        if len(coords) < 3:
            return
        name = w.tags.get("name")
        self.parks.append(
            {"osm_id": w.id, "park_type": pt, "coords": coords, "name": name}
        )

    def relation(self, r):
        pt = self._park_type(r.tags)
        if not pt:
            return
        # Extract outer members (simple approach: first outer way's coords)
        outer_coords = None
        for m in r.members:
            if m.role == "outer" and m.type == "w":
                try:
                    w = osmium._osmium.Way(m.ref)
                    coords = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
                    if len(coords) >= 3:
                        outer_coords = coords
                        break
                except:
                    pass
        if not outer_coords:
            return
        name = r.tags.get("name")
        self.parks.append(
            {"osm_id": r.id, "park_type": pt, "coords": outer_coords, "name": name}
        )

    @staticmethod
    def _park_type(tags):
        for tag in tags:
            if tag.k in PARK_TAGS and tag.v in PARK_TAGS[tag.k]:
                return tag.v
        return None


def encode_coordinates(coords):
    if not coords:
        return b""
    buf = bytearray()
    prev_lon = round(coords[0][0] * 100_000)
    prev_lat = round(coords[0][1] * 100_000)
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
    n = len(feat["coords"])
    buf.append(n if n < 256 else 255)
    if n >= 256:
        buf.append(n & 0xFF)
        buf.append((n >> 8) & 0xFF)
    buf.extend(
        struct.pack(
            "<ii",
            round(feat["coords"][0][0] * 100000),
            round(feat["coords"][0][1] * 100000),
        )
    )
    buf.extend(encode_coordinates(feat["coords"]))
    # Park type as u8 string
    nb = feat["park_type"].encode("utf-8")
    buf.append(len(nb))
    buf.extend(nb)
    # Name
    flags = 0
    if feat.get("name"):
        flags |= 0x01
    buf.append(flags)
    if feat.get("name"):
        nb2 = feat["name"].encode("utf-8")
        buf.extend(struct.pack("<H", len(nb2)))
        buf.extend(nb2)
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

    h = ParkHandler()
    h.apply_file(str(pbfp), locations=True)
    parks = h.parks
    if not parks:
        return {"abbr": abbr, "features": 0, "time_s": round(time.time() - t0, 1)}

    per_cell = defaultdict(list)
    for p in parks:
        try:
            cell = h3.latlng_to_cell(p["coords"][0][1], p["coords"][0][0], H3_RES)
            p["cell"] = int(cell, 16) if isinstance(cell, str) else cell
            per_cell[p["cell"]].append(p)
        except:
            continue
    for c in per_cell:
        per_cell[c].sort(key=lambda p: p["osm_id"])

    sc = sorted(per_cell.keys())
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
            for p in per_cell[cell]:
                rec = enc(p, pid)
                pid = p["osm_id"]
                rs.append(rec)
            cr.append((cell, rs))
            pd.append((cell, len(rs)))
        if not cr:
            continue
        clat, clon = h3.cell_to_latlng(hex(bch[0])[2:])
        blk = encode_merged_block(cr, round(clon * 100000), round(clat * 100000))
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
                        min(p["coords"][0][0] for p in per_cell[cell]) * 100000
                    ),
                    "min_lat": round(
                        min(p["coords"][0][1] for p in per_cell[cell]) * 100000
                    ),
                    "max_lon": round(
                        max(p["coords"][0][0] for p in per_cell[cell]) * 100000
                    ),
                    "max_lat": round(
                        max(p["coords"][0][1] for p in per_cell[cell]) * 100000
                    ),
                }
            )
            off += 1

    if not mb:
        return {"abbr": abbr, "features": 0}
    dd = b""
    cbs = [zstd.ZstdCompressor(level=1).compress(b) for b in mb]
    tf = sum(e["feature_count"] for e in pi)
    do = HEADER_SIZE
    dl = 0
    io = do
    il = 4 + len(pi) * INDEX_ENTRY_SIZE_V2
    bo = io + il
    ie = []
    for idx, pc_ in enumerate(pi):
        bi = idx // bs
        ie.append(
            {
                **pc_,
                "block_offset": bo + sum(len(cb) for cb in cbs[:bi]),
                "block_length": len(cbs[bi]) if bi < len(cbs) else 0,
            }
        )
    op = OUTPUT_DIR / f"{abbr}.parks.ptiles"
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
        "cells": len(per_cell),
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
                    f"  {r['abbr']:2s} {r['features']:6d} parks {r['bytes']:10,d} B  {r['time_s']:6.1f}s",
                    flush=True,
                )
            elif r:
                print(f"  {r['abbr']:2s}  0", flush=True)
        except Exception as e:
            print(f"  ERROR {abbr}: {e}", flush=True)
            import traceback

            traceback.print_exc()


if __name__ == "__main__":
    main()
