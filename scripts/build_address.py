#!/usr/bin/env python3
"""
Build .address.ptiles from per-state OSM PBF files.
Per-state files for memory efficiency using SimpleHandler + locations=True.
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
    encode_merged_block,
    write_header,
    HEADER_SIZE,
    encode_index_entry_v2,
    INDEX_ENTRY_SIZE_V2,
)
from states import STATES, get_state

OUTPUT_DIR = Path("/mnt/core/kino/ptiles/data/v4/states")
PBF_DIR = Path("/mnt/core/timeline-ptiles-cache/raw")
# PTILESD per SPEC.md. This was b"PTILESA2\x00" — nine bytes, of which
# write_header keeps only the first seven, so the "2" was dropped and every
# address file shipped carrying PTILESA, the *admin* magic. Files built before
# this fix are indistinguishable from admin files by their magic byte and need
# rebuilding to be identified correctly.
MAGIC = b"PTILESD\x00"

# v2 adds i16 cell-relative coordinates to each record.
#
# v1 stored only (osm_id, housenumber, street). The extractor had the position
# — it needs one to pick the H3 cell — and then discarded it, so an address
# could only ever be located to its cell. Measured on the v1 TN file, that is a
# median 531 m of uncertainty and 2.6 km at p90, which cannot support either
# direction of geocoding. Four bytes per record fixes it.
VERSION = 2
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


class AddrExtractor(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.addrs = []

    def way(self, w):
        hn = st = None
        has_building = False
        for t in w.tags:
            if t.k == "building" and t.v:
                has_building = True
            elif t.k == "addr:housenumber" and t.v:
                hn = t.v
            elif t.k == "addr:street" and t.v:
                st = t.v
        if not has_building or not hn:
            return
        if not w.nodes:
            return
        lon = lat = None
        for n in w.nodes:
            if n.location.valid():
                lon, lat = n.lon, n.lat
                break
        if lon is None:
            return
        try:
            cell = h3.latlng_to_cell(lat, lon, H3_RES)
        except:
            return
        self.addrs.append(
            {
                "osm_id": w.id,
                "lon": lon,
                "lat": lat,
                "housenumber": hn,
                "street": st or "",
                "cell": int(cell, 16) if isinstance(cell, str) else cell,
            }
        )


def enc(a, pid, cell_center_micro):
    """Encode one v2 address record.

    Coordinates are i16 offsets in microdegrees from the centre of the H3 cell
    the address hashes to, the same scheme buildings and business v4 use. A
    res-7 cell spans roughly 0.02 degrees against the i16 ceiling of 0.32768,
    so the offset cannot overflow for a point inside its own cell — which is
    the only way records are ever grouped here.
    """
    b = bytearray()
    b.extend(encode_varint(zigzag_encode(a["osm_id"] - pid)))
    off_lon = round(a["lon"] * 100000) - cell_center_micro[0]
    off_lat = round(a["lat"] * 100000) - cell_center_micro[1]
    b.extend(struct.pack("<hh", off_lon, off_lat))
    hn = a["housenumber"].encode("utf-8")
    b.extend(struct.pack("<H", len(hn)))
    b.extend(hn)
    st = a["street"].encode("utf-8")
    b.extend(struct.pack("<H", len(st)))
    b.extend(st)
    return bytes(b)


def build(abbr):
    pbfn = PBF_MAP.get(abbr)
    if not pbfn:
        return {"abbr": abbr, "error": "no mapping"}
    pbfp = PBF_DIR / f"{pbfn}.osm.pbf"
    if not pbfp.exists():
        return {"abbr": abbr, "error": "no pbf"}
    s = get_state(abbr)
    t0 = time.time()

    h = AddrExtractor()
    h.apply_file(str(pbfp), locations=True)
    addrs = h.addrs
    if not addrs:
        return {"abbr": abbr, "addrs": 0, "time_s": round(time.time() - t0, 1)}

    pc = defaultdict(list)
    for a in addrs:
        pc[a["cell"]].append(a)
    for c in pc:
        pc[c].sort(key=lambda a: a["osm_id"])
    sc = sorted(pc.keys())
    mb, pi, bs = [], [], 8
    for i in range(0, len(sc), bs):
        bch = sc[i : i + bs]
        cr = []
        pd = []
        for cell in bch:
            # Offsets are measured from each cell's own centre, not the block's
            # — a block spans 8 cells, and per-cell keeps the deltas small and
            # the decode independent of how cells were batched.
            ccl, ccn = h3.cell_to_latlng(hex(cell)[2:])
            cell_center_micro = (round(ccn * 100000), round(ccl * 100000))
            rs, pid = [], 0
            for a in pc[cell]:
                rs.append(enc(a, pid, cell_center_micro))
                pid = a["osm_id"]
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
                    "feature_count": cnt,
                    "cell_index": off,
                    "min_lon": round(min(a["lon"] for a in pc[cell]) * 100000),
                    "min_lat": round(min(a["lat"] for a in pc[cell]) * 100000),
                    "max_lon": round(max(a["lon"] for a in pc[cell]) * 100000),
                    "max_lat": round(max(a["lat"] for a in pc[cell]) * 100000),
                }
            )
            off += 1

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

    op = OUTPUT_DIR / f"{abbr}.address_v2.ptiles"
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
    return {
        "abbr": abbr,
        "addrs": tf,
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
        p.print_help()
        return
    for abbr in targets:
        try:
            r = build(abbr)
            if r and r.get("addrs"):
                print(
                    f"  {r['abbr']:2s} {r['addrs']:8,d} addrs  {r['bytes']:10,d} B  {r['time_s']:6.1f}s",
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
