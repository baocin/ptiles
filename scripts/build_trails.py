#!/usr/bin/env python3
"""
Build trails.ptiles from per-state OSM PBF files.

Uses osmium FileProcessor + KeyFilter for fast trail extraction.
Format: PTILESH magic, v1, v2 merged-block -- the same shape as build_rail.py.

Captures:
  - Trail ways: path, track, bridleway, cycleway, steps, and footway
  - Trailhead nodes: highway=trailhead

`footway` is deliberately conditional. It outnumbers every other trail tag
roughly 5:1 (Tennessee: 127,540 of 153,134 trail-ish ways), and nearly all of
that is sidewalks and street crossings, which are pedestrian infrastructure
rather than trails. OSM marks those with the `footway=sidewalk|crossing`
subtag, so a footway is admitted only when it carries no such subtag -- that
keeps park and woodland footpaths and drops the sidewalk network.
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

from encoding import coord_to_micro, micro_to_coord
from shared import (
    encode_varint,
    zigzag_encode,
    encode_index_entry_v2,
    INDEX_ENTRY_SIZE_V2,
    encode_merged_block,
    write_header,
    HEADER_SIZE,
)
from boundaries import stamp_boundary
from states import STATES, get_state, pbf_path as find_pbf, scopes_for_country

OUTPUT_DIR = Path("/mnt/core/kino/ptiles/data/states")
PBF_DIR = Path("/mnt/core/timeline-ptiles-cache/2026-08-06/pbf")
MAGIC = b"PTILESH\x00"
VERSION = 2  # v2 adds name:en and brand (0x02/0x04)
H3_RES = 7

# Way types captured as linestrings. Order is the on-disk type index; append
# only, never reorder, or older readers mislabel every feature.
TRAIL_TYPES = [
    "path",
    "track",
    "bridleway",
    "cycleway",
    "footway",
    "steps",
]
# Node types captured as points.
NODE_TYPES = ["trailhead"]

TT = TRAIL_TYPES + NODE_TYPES
TTI = {t: i for i, t in enumerate(TT)}

# footway carrying one of these is street furniture, not a trail.
URBAN_FOOTWAY = {"sidewalk", "crossing", "traffic_island"}

SURFACES = [
    "",
    "paved",
    "asphalt",
    "concrete",
    "gravel",
    "compacted",
    "fine_gravel",
    "dirt",
    "ground",
    "grass",
    "sand",
    "wood",
    "boardwalk",
]
SURFACE_IDX = {s: i for i, s in enumerate(SURFACES)}

# SAC hiking scale, coarse difficulty. 0 = unset.
SAC = [
    "",
    "hiking",
    "mountain_hiking",
    "demanding_mountain_hiking",
    "alpine_hiking",
    "demanding_alpine_hiking",
    "difficult_alpine_hiking",
]
SAC_IDX = {s: i for i, s in enumerate(SAC)}


def extract(pbf):
    import osmium

    # with_locations() is required: without a node cache every way node reports
    # an invalid location and every way is dropped. This is the bug that left
    # the rail layer holding stations only.
    fp = (
        osmium.FileProcessor(pbf)
        .with_locations()
        .with_filter(osmium.filter.KeyFilter("highway"))
    )
    features = []
    for obj in fp:
        is_node = hasattr(obj, "lat")
        tags = {t.k: t.v for t in obj.tags}
        h = tags.get("highway")

        if is_node:
            if h != "trailhead":
                continue
            trail_type = "trailhead"
        else:
            if h not in TRAIL_TYPES:
                continue
            if h == "footway" and tags.get("footway") in URBAN_FOOTWAY:
                continue
            trail_type = h

        if is_node:
            key_coord = (obj.lon, obj.lat)
            coords = [key_coord]
        else:
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

        try:
            cell = h3.latlng_to_cell(key_coord[1], key_coord[0], H3_RES)
        except Exception:
            continue

        features.append(
            {
                "osm_id": obj.id,
                "trail_type": trail_type,
                "geom_type": 1 if is_node else 0,
                "coords": coords,
                "name": tags.get("name"),
                "name_en": tags.get("name:en"),
                "brand": tags.get("brand"),
                "surface": SURFACE_IDX.get(tags.get("surface", ""), 0),
                "sac": SAC_IDX.get(tags.get("sac_scale", ""), 0),
                "cell": int(cell, 16) if isinstance(cell, str) else cell,
            }
        )

    return features


def encode_coordinates(coords):
    """Encode coordinate sequence as varint deltas."""
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
    buf.append(feat["geom_type"])  # 0=linestring, 1=point
    coords = feat.get("coords", [])
    if feat["geom_type"] == 0:
        if len(coords) < 2:
            return b""
        buf.extend(struct.pack("<H", len(coords)))
        buf.extend(
            struct.pack(
                "<ii", round(coords[0][0] * 100000), round(coords[0][1] * 100000)
            )
        )
        buf.extend(encode_coordinates(coords))
    else:
        pt = coords[0] if coords else (0, 0)
        buf.extend(struct.pack("<ii", round(pt[0] * 100000), round(pt[1] * 100000)))
    buf.append(TTI.get(feat["trail_type"], 0))
    buf.append(feat["surface"])
    buf.append(feat["sac"])
    flags = 0x01 if feat.get("name") else 0
    if feat.get("name_en"):
        flags |= 0x02
    if feat.get("brand"):
        flags |= 0x04
    if feat.get("park_osm_id"):
        flags |= 0x08
    buf.append(flags)
    if feat.get("name"):
        nb = feat["name"].encode("utf-8")
        buf.extend(struct.pack("<H", len(nb)))
        buf.extend(nb)
    # v2 fields after every v1 field, so the prefix layout is untouched. These
    # records are not length-prefixed, so a v1 reader must not read a v2 file --
    # that is what the version bump is for.
    for key in ("name_en", "brand"):
        if feat.get(key):
            vb = feat[key].encode("utf-8")
            buf.extend(struct.pack("<H", len(vb)))
            buf.extend(vb)
    # The park this trail starts inside, when it starts inside one. A trail can
    # enter and leave a park, so this is a claim about the first vertex, not the
    # whole way -- see the reader's docstring.
    if feat.get("park_osm_id"):
        buf.extend(encode_varint(feat["park_osm_id"]))
    return bytes(buf)



def load_park_index(abbr):
    """Grid index of park polygons for a scope, or None when parks are absent.

    Reads the already-built parks file rather than re-extracting polygons from
    the PBF -- the parks layer has the geometry and is cell-indexed already.
    That makes this a build-order dependency: parks must be built before trails
    or the association is simply missing, which is why it says so out loud.

    Keyed by the 0.02-degree cell of every park vertex, so a park is found from
    anywhere it reaches rather than only from where its first vertex sits.
    """
    import glob

    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from ptiles.parks import ParkReader
    except Exception as e:
        print(f"  parks: reader unavailable ({e}); trails will carry no park id",
              flush=True)
        return None

    found = sorted(glob.glob(str(OUTPUT_DIR / f"{abbr}.parks_v*.ptiles")))
    if not found:
        print(f"  parks: no {abbr}.parks_*.ptiles built yet; trails will carry "
              f"no park id (build parks first)", flush=True)
        return None

    reader = ParkReader.open(found[-1])
    grid = {}
    count = 0
    unreadable = 0
    for entry in reader._index:
        # One malformed cell must not take the whole trails build down with it.
        # It did: a parks encoding bug desynced two cells out of 110,991 and the
        # exception propagated out of here, failing the entire stage rather than
        # costing a handful of park associations.
        try:
            parks_in_cell = reader.get_in_cell(entry["h3_cell"])
        except Exception:
            unreadable += 1
            continue
        for park in parks_in_cell:
            if not park.coords or len(park.coords) < 3:
                continue
            ring = tuple(park.coords)
            count += 1
            keys = {
                (int(lon * 50), int(lat * 50))  # 0.02 deg buckets
                for lon, lat in ring
            }
            for key in keys:
                grid.setdefault(key, []).append((park.osm_id, ring))
    if unreadable:
        print(f"  parks: WARNING {unreadable} cells unreadable and skipped",
              flush=True)
    print(f"  parks: indexed {count} polygons from {Path(found[-1]).name}",
          flush=True)
    return grid


def park_containing(grid, lon, lat):
    """osm_id of the park whose polygon contains this point, or None."""
    if not grid:
        return None
    from ptiles.geometry import point_in_polygon

    for osm_id, ring in grid.get((int(lon * 50), int(lat * 50)), ()):
        if point_in_polygon(lon, lat, ring):
            return osm_id
    return None


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

    # Which park each trail starts in. Answerable at runtime by testing the
    # parks layer, but storing it means a client asking "is this trail in a
    # park" does not have to hold parks open at all.
    park_grid = load_park_index(abbr)
    if park_grid:
        tagged = 0
        for feat in features:
            coords = feat.get("coords") or []
            if not coords:
                continue
            # Test the coordinates that will actually be stored, not the raw
            # ones: geometry is quantised to 1e5 on write, and a first vertex
            # within ~1 m of a park edge otherwise lands inside at build time
            # and outside at read time. Same trap as the H3 cell assignment in
            # build_state_v8.
            _lon = micro_to_coord(coord_to_micro(coords[0][0]))
            _lat = micro_to_coord(coord_to_micro(coords[0][1]))
            pid = park_containing(park_grid, _lon, _lat)
            if pid:
                feat["park_osm_id"] = pid
                tagged += 1
        print(f"  parks: {tagged} of {len(features)} trails start inside a park",
              flush=True)

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
            # Cell bbox over every vertex, not just each feature's first point:
            # a trail entering a cell and leaving it would otherwise report a
            # box that stops at its start node.
            xs = [c[0] for f in pc[cell] for c in f["coords"]]
            ys = [c[1] for f in pc[cell] for c in f["coords"]]
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

    # Header bounds measured from the data, not from the state box: a state's
    # trails do not reach its corners, and a declared box wider than the data
    # sends the client fetching cells that hold nothing.
    all_x = [e for entry in pi for e in (entry["min_lon"], entry["max_lon"])]
    all_y = [e for entry in pi for e in (entry["min_lat"], entry["max_lat"])]

    op = OUTPUT_DIR / f"{abbr}.trails_v{VERSION}.ptiles"
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
    stamp_boundary(op, s)

    return {
        "abbr": abbr,
        "features": tf,
        "cells": len(pc),
        "bytes": op.stat().st_size,
        "time_s": round(time.time() - t0, 1),
    }


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true")
    p.add_argument("--states")
    p.add_argument(
        "--countries",
        help="Comma-separated countries, e.g. JP -- expands to every declared "
             "scope of that country (JP plus its 8 regions)",
    )
    args = p.parse_args()

    targets = []
    if args.all:
        targets = [s.abbr for s in STATES]
    elif args.states:
        for a in args.states.split(","):
            s = get_state(a.strip())
            if s:
                targets.append(s.abbr)
    elif args.countries:
        for c in args.countries.split(","):
            found = scopes_for_country(c.strip())
            if found:
                targets.extend(found)
            else:
                print(f"No declared scopes for country: {c.strip()}")
    else:
        p.print_help()
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for abbr in targets:
        try:
            r = build_state(abbr)
            if r.get("features"):
                print(
                    f"  {r['abbr']:2s} {r['features']:7d} trails  {r['cells']:5d} cells  "
                    f"{r['bytes']:10,d} B  {r['time_s']:6.1f}s",
                    flush=True,
                )
            else:
                print(f"  {r['abbr']:2s}  0  ({r.get('error', 'no features')})", flush=True)
        except Exception as e:
            print(f"  ERROR {abbr}: {e}", flush=True)
            import traceback

            traceback.print_exc()

    print("TRAILS_COMPLETE")


if __name__ == "__main__":
    main()
