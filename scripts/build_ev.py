#!/usr/bin/env python3
"""
Build ev.ptiles from per-state OSM PBF files: public EV charging stations.

Format: PTILESE magic, v1, v2 merged blocks -- the same shape as trails and
rail, so the client's existing block reader handles it unchanged.

Source is `amenity=charging_station` in the same Geofabrik state extracts every
other layer is built from. No new dependency and no API: OSM's charging-station
coverage is the thing the rest of this dataset is already made of, and a
second source (NREL AFDC, say) would need its own licence, its own refresh
cadence and its own reconciliation against these ids.

A station is a *site*, not a plug. OSM models it as a node most of the time and
as a building/parking-aisle way sometimes; a way is reduced to the centroid of
its nodes, because what a router wants is somewhere to drive to.

Captured per station: position, access, peak power in kW, how many vehicles it
can take at once, which connectors it has, and the network that runs it. Power
and connector are what decide whether a given car can actually charge there,
and they are the two things most often untagged -- so both carry an explicit
"unknown" rather than a plausible default. A router that treats unknown as
"fine" strands people; one that treats it as "unusable" hides most of Wyoming.
Reporting it as unknown lets the caller decide which mistake to make.
"""

import os
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
from states import STATES, get_state, pbf_path as find_pbf

# data/states is where the other builders write, but it is root-owned and
# empty on this host (its contents are published and were cleared), so the
# path is overridable. Point PTILES_OUT at scratch to build without touching
# the repo -- 51 states of tiles do not belong in git.
OUTPUT_DIR = Path(os.environ.get("PTILES_OUT", "/mnt/core/kino/ptiles/data/states"))
PBF_DIR = Path("/mnt/core/timeline-ptiles-cache/2026-08-06/pbf")
MAGIC = b"PTILESE\x00"
VERSION = 1
H3_RES = 7

# On-disk index for `access`. Append only, never reorder.
ACCESS = ["unknown", "yes", "customers", "permissive", "private", "no"]
ACCESS_IDX = {a: i for i, a in enumerate(ACCESS)}

# Connector bitmask. One bit per socket kind, in this order forever: a reader
# maps bit position to meaning, so inserting in the middle would rename every
# connector on every previously published file.
SOCKETS = [
    "type1",           # J1772, the North American AC standard
    "type1_combo",     # CCS1
    "type2",           # Mennekes
    "type2_combo",     # CCS2
    "type2_cable",
    "chademo",
    "tesla_supercharger",
    "tesla_destination",
    "tesla_supercharger_ccs",
    "nema_5_15",
    "nema_5_20",
    "nema_14_50",
    "schuko",
]
SOCKET_BIT = {s: 1 << i for i, s in enumerate(SOCKETS)}


def parse_kw(value):
    """Leading number of a power tag, in kW.

    OSM writes these as free text -- "22", "22 kW", "50kw", "3.7 kW AC" -- and
    occasionally in watts. Values above 1000 are read as watts, since no
    roadside charger delivers a megawatt and plenty are tagged "22000".
    """
    if not value:
        return 0.0
    num = ""
    for ch in value.strip():
        if ch.isdigit() or (ch == "." and "." not in num):
            num += ch
        elif num:
            break
        elif ch in "+-":
            continue
        else:
            break
    if not num:
        return 0.0
    try:
        kw = float(num)
    except ValueError:
        return 0.0
    if kw > 1000.0:
        kw /= 1000.0
    return kw


def station_power_kw(tags):
    """Peak kW the site advertises, 0 when nothing says.

    The per-socket `socket:*:output` tags are the reliable ones and the max
    across them is what a driver cares about: a site with a 150 kW CCS and a
    7 kW type 2 is a 150 kW stop. `charge` and `maxpower` are the fallbacks.
    """
    best = 0.0
    for k, v in tags.items():
        if k.startswith("socket:") and k.endswith(":output"):
            best = max(best, parse_kw(v))
    if best == 0.0:
        for k in ("charge", "maxpower", "rated_power", "power"):
            best = max(best, parse_kw(tags.get(k)))
    return best


def station_sockets(tags):
    """Connector bitmask from the `socket:<kind>` tags.

    The value is a count and is ignored here -- a `socket:type2=4` says the
    site has four of them, which `capacity` already covers. Presence is the
    question this field answers.
    """
    mask = 0
    for kind, bit in SOCKET_BIT.items():
        v = tags.get(f"socket:{kind}")
        if v is None:
            continue
        if v.strip().lower() in ("no", "0", "none"):
            continue
        mask |= bit
    return mask


def station_capacity(tags):
    """Vehicles the site can charge at once. 0 = untagged."""
    for k in ("capacity", "charging_station:capacity"):
        v = tags.get(k)
        if not v:
            continue
        digits = "".join(ch for ch in v if ch.isdigit())
        if digits:
            return min(int(digits), 255)
    return 0


def extract(pbf):
    import osmium

    # with_locations(): a station mapped as a way (a parking aisle, a canopy)
    # has no position of its own, and without the node cache every one of them
    # is dropped -- the same bug that once left rail holding stations only.
    fp = (
        osmium.FileProcessor(pbf)
        .with_locations()
        .with_filter(osmium.filter.KeyFilter("amenity"))
    )
    features = []
    for obj in fp:
        tags = {t.k: t.v for t in obj.tags}
        if tags.get("amenity") != "charging_station":
            continue

        # Motorcycle- and bicycle-only points are charging stations in OSM's
        # sense and useless to a car router. Excluded only when the tags say
        # so outright; an untagged station is assumed to take cars.
        if tags.get("motorcar") == "no" or tags.get("motor_vehicle") == "no":
            continue

        if hasattr(obj, "lat"):
            lon, lat = obj.lon, obj.lat
        else:
            if not hasattr(obj, "nodes"):
                continue
            pts = [(n.lon, n.lat) for n in obj.nodes if n.location.valid()]
            if not pts:
                continue
            lon = sum(p[0] for p in pts) / len(pts)
            lat = sum(p[1] for p in pts) / len(pts)

        try:
            cell = h3.latlng_to_cell(lat, lon, H3_RES)
        except Exception:
            continue

        kw = station_power_kw(tags)
        features.append(
            {
                "osm_id": obj.id,
                "lon": lon,
                "lat": lat,
                "access": ACCESS_IDX.get(tags.get("access", "unknown"), 0),
                # Tenths of a kW in a u16: 0.1 kW resolution up to 6553 kW,
                # which is far past any charger and keeps the field two bytes.
                "power_dkw": min(round(kw * 10), 65535),
                "capacity": station_capacity(tags),
                "sockets": station_sockets(tags),
                "name": tags.get("name"),
                "network": tags.get("network") or tags.get("operator") or tags.get("brand"),
                "ref": tags.get("ref"),
                "cell": int(cell, 16) if isinstance(cell, str) else cell,
            }
        )

    return features


def enc(feat, pid):
    buf = bytearray()
    buf.extend(encode_varint(zigzag_encode(feat["osm_id"] - pid)))
    buf.extend(
        struct.pack("<ii", round(feat["lon"] * 100000), round(feat["lat"] * 100000))
    )
    buf.append(feat["access"])
    buf.extend(struct.pack("<H", feat["power_dkw"]))
    buf.append(feat["capacity"])
    buf.extend(struct.pack("<H", feat["sockets"]))

    name = feat.get("name")
    network = feat.get("network")
    ref = feat.get("ref")
    flags = (0x01 if name else 0) | (0x02 if network else 0) | (0x04 if ref else 0)
    buf.append(flags)
    if name:
        nb = name.encode("utf-8")[:65535]
        buf.extend(struct.pack("<H", len(nb)))
        buf.extend(nb)
    if network:
        ob = network.encode("utf-8")[:255]
        buf.append(len(ob))
        buf.extend(ob)
    if ref:
        rb = ref.encode("utf-8")[:255]
        buf.append(len(rb))
        buf.extend(rb)
    return bytes(buf)


def build_state(abbr):
    s = get_state(abbr)
    if not s:
        return {"abbr": abbr, "error": "unknown state"}
    pbfp = find_pbf(s, prefer=PBF_DIR)
    if pbfp is None:
        return {"abbr": abbr, "error": "no pbf extract found"}
    t0 = time.time()

    features = extract(str(pbfp))
    if not features:
        return {"abbr": abbr, "features": 0, "time_s": round(time.time() - t0, 1)}

    pc = defaultdict(list)
    for f in features:
        pc[hex(f["cell"])[2:]].append(f)
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
            pd.append((cell, len(rs)))
        if not cr:
            continue
        cla, clo = h3.cell_to_latlng(bch[0])
        blk = encode_merged_block(cr, round(clo * 100000), round(cla * 100000))
        mb.append(blk)
        off = 0
        for cell, cnt in pd:
            xs = [f["lon"] for f in pc[cell]]
            ys = [f["lat"] for f in pc[cell]]
            pi.append(
                {
                    "h3_cell": int(cell, 16),
                    "block_offset": 0,
                    "block_length": len(blk),
                    "feature_count": cnt,
                    "cell_index": off,
                    "min_lon": round(min(xs) * 100000),
                    "min_lat": round(min(ys) * 100000),
                    "max_lon": round(max(xs) * 100000),
                    "max_lat": round(max(ys) * 100000),
                }
            )
            off += 1

    if not mb:
        return {"abbr": abbr, "features": 0}

    dd = b""
    cbs = [zstd.ZstdCompressor(level=3).compress(b) for b in mb]

    tf = sum(e["feature_count"] for e in pi)
    do = HEADER_SIZE
    dl = len(dd)
    io = do + dl
    il = 4 + len(pi) * INDEX_ENTRY_SIZE_V2
    bo = io + il

    ie = []
    for idx, entry in enumerate(pi):
        block_idx = idx // bs
        ie.append(
            {
                **entry,
                "block_offset": bo + sum(len(cb) for cb in cbs[:block_idx]),
                "block_length": len(cbs[block_idx]) if block_idx < len(cbs) else 0,
            }
        )

    # Bounds from the data, not the state box: chargers cluster on corridors
    # and a wider declared box sends the client fetching empty cells.
    all_x = [e for entry in pi for e in (entry["min_lon"], entry["max_lon"])]
    all_y = [e for entry in pi for e in (entry["min_lat"], entry["max_lat"])]

    op = OUTPUT_DIR / f"{abbr}.ev_v{VERSION}.ptiles"
    with open(op, "wb") as f:
        write_header(
            f,
            MAGIC,
            VERSION,
            min(all_y) / 100000,
            min(all_x) / 100000,
            max(all_y) / 100000,
            max(all_x) / 100000,
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

    powered = sum(1 for f in features if f["power_dkw"] > 0)
    return {
        "abbr": abbr,
        "features": tf,
        "cells": len(pc),
        "powered": powered,
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

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for abbr in targets:
        try:
            r = build_state(abbr)
            if r.get("features"):
                print(
                    f"  {r['abbr']:2s} {r['features']:6d} chargers  {r['cells']:5d} cells  "
                    f"{r['powered']:6d} with power  {r['bytes']:9,d} B  {r['time_s']:6.1f}s",
                    flush=True,
                )
            else:
                print(f"  {r['abbr']:2s}  0  ({r.get('error', 'no features')})", flush=True)
        except Exception as e:
            print(f"  ERROR {abbr}: {e}", flush=True)
            import traceback

            traceback.print_exc()

    print("EV_COMPLETE")


if __name__ == "__main__":
    main()
