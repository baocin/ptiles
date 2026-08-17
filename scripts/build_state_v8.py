#!/usr/bin/env python3
"""
Build per-state PTILES v8 buildings from per-state OSM PBF.

Usage:
    python build_state_v8.py TN
    python build_state_v8.py --all
"""

import sys
import os
import time
from array import array
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
import osmium
import h3

from shared import (
    write_header,
    HEADER_SIZE,
    write_index,
    train_dictionary,
    compress_block,
)
from encode_v8 import (
    METERS_PER_LEVEL,
    encode_block_v8,
    parse_height,
    parse_levels,
)
from encoding import coord_to_micro, micro_to_coord
from boundaries import stamp_boundary
from states import (
    STATES,
    get_state,
    state_bbox,
    scopes_for_country,
    pbf_path as find_pbf,
)

PBF_DIR = Path("/mnt/core/timeline-ptiles-cache/raw")
OUTPUT_DIR = Path("/mnt/core/kino/ptiles/data/v4/states")
H3_RES = 7
MAGIC = b"PTILESF\x00"
VERSION = 9


def as_dict(b):
    """Compact building tuple back to the dict encode_block_v8 expects."""
    (osm_id, ring, btype, height, name, shop, amenity_val, opening_hours,
     name_en, brand, alt_name) = b
    d = {
        "osm_id": osm_id,
        "coords": [
            [micro_to_coord(ring[i]), micro_to_coord(ring[i + 1])]
            for i in range(0, len(ring), 2)
        ],
        "building_type": btype,
        "height_m": height,
    }
    if name:
        d["name"] = name
    if shop:
        d["shop"] = shop
    if amenity_val:
        d["amenity"] = amenity_val
    if opening_hours:
        d["opening_hours"] = opening_hours
    if name_en:
        d["name_en"] = name_en
    if brand:
        d["brand"] = brand
    if alt_name:
        d["alt_name"] = alt_name
    return d


def _lon_within(west: float, east: float, lon: float) -> bool:
    """Longitude containment that survives the antimeridian.

    A region crossing 180 has west > east, and the naive `west <= lon <= east`
    is then false everywhere -- which here would silently drop every feature
    rather than merely mis-filter. states.py gives Alaska the whole globe to
    dodge this; with the wrap handled, a real bbox works.
    """
    if west <= east:
        return west <= lon <= east
    return lon >= west or lon <= east


class BuildingHandler(osmium.SimpleHandler):
    def __init__(self, state_bbox):
        super().__init__()
        self.min_lon, self.min_lat, self.max_lon, self.max_lat = state_bbox
        self.buildings = []

    def way(self, w):
        if not any(tag.k == "building" for tag in w.tags if tag.v):
            return
        if not w.nodes:
            return
        try:
            ring = array("i")  # interleaved micro-degrees; see module docstring
            for node in w.nodes:
                try:
                    lat = node.location.lat
                    lon = node.location.lon
                except Exception:
                    continue
                if not ring:
                    if not (
                        _lon_within(self.min_lon, self.max_lon, lon)
                        and self.min_lat <= lat <= self.max_lat
                    ):
                        return
                ring.append(coord_to_micro(lon))
                ring.append(coord_to_micro(lat))
            if len(ring) < 8:  # fewer than 4 vertices
                return
            if ring[0] != ring[-2] or ring[1] != ring[-1]:
                ring.append(ring[0])
                ring.append(ring[1])

            btype = "yes"
            name = None
            height = None
            levels = None
            shop = None
            amenity_val = None
            opening_hours = None
            name_en = None
            brand = None
            alt_name = None
            for tag in w.tags:
                if tag.k == "building" and tag.v:
                    btype = tag.v
                elif tag.k == "name":
                    name = tag.v
                elif tag.k == "height":
                    height = parse_height(tag.v)
                elif tag.k == "building:levels":
                    levels = parse_levels(tag.v)
                elif tag.k == "shop" and tag.v:
                    shop = tag.v
                elif tag.k == "amenity" and tag.v:
                    amenity_val = tag.v
                elif tag.k == "opening_hours" and tag.v:
                    opening_hours = tag.v
                elif tag.k == "name:en" and tag.v:
                    name_en = tag.v
                elif tag.k == "brand" and tag.v:
                    brand = tag.v
                elif tag.k == "alt_name" and tag.v:
                    alt_name = tag.v

            # An explicit height always wins; levels are only a fallback.
            if height is None and levels is not None:
                height = levels * METERS_PER_LEVEL

            self.buildings.append(
                (w.id, ring, btype, height, name, shop, amenity_val, opening_hours,
                 name_en, brand, alt_name)
            )
        except Exception:
            pass


def build_state_pbf(state):
    print(f"\n=== {state.abbr} {state.name} ===", flush=True)
    t0 = time.time()

    pbf_path = find_pbf(state, prefer=PBF_DIR)
    if pbf_path is None:
        print(f"  No PBF extract found for {state.abbr}")
        return

    bbox = state_bbox(state)
    handler = BuildingHandler(bbox)
    handler.apply_file(str(pbf_path), locations=True)

    bldgs = handler.buildings
    if not bldgs:
        print("  No buildings found", flush=True)
        return

    print(f"  Extracted {len(bldgs)} buildings", flush=True)
    bldgs.sort(key=lambda b: b[0])

    # Group by H3 cell. The cell comes from the quantized first vertex, i.e. the
    # coordinates actually stored in the file, so a building is always indexed
    # under the cell its own stored geometry falls in. Against the pre-quantized
    # float this moves ~0.02% of buildings (those within ~1m of a cell edge).
    cells = defaultdict(list)
    for b in bldgs:
        ring = b[1]
        cell = h3.latlng_to_cell(micro_to_coord(ring[1]), micro_to_coord(ring[0]), H3_RES)
        cells[int(cell, 16)].append(b)
    print(f"  Grouped into {len(cells)} H3 cells", flush=True)

    # Encode blocks
    sorted_cells = sorted(cells.keys())
    raw_blocks = {}
    total_features = 0
    index_entries = []
    for cell in sorted_cells:
        block_bytes, count = encode_block_v8([as_dict(b) for b in cells[cell]], cell)
        raw_blocks[cell] = block_bytes
        total_features += count
        # NOTE: index_entries populated after compression (need block sizes)

    print(
        f"  Encoded {total_features} features in {len(raw_blocks)} blocks", flush=True
    )

    # Train dict and compress
    samples = list(raw_blocks.values())[:2000]
    dict_data = train_dictionary(samples)
    compressed = {}
    for cell in sorted_cells:
        compressed[cell] = compress_block(raw_blocks[cell], dict_data)

    # Build header
    dict_offset = HEADER_SIZE
    dict_length = len(dict_data)
    index_offset = dict_offset + dict_length

    # Build index entries — track running block offset relative to blocks_offset
    cur_block_off = 0
    for cell in sorted_cells:
        blen = len(compressed[cell])
        index_entries.append(
            {
                "h3_cell": cell,
                "block_offset": cur_block_off,
                "block_length": blen,
                "feature_count": len(cells[cell]),
            }
        )
        cur_block_off += blen

    index_length = 4 + len(index_entries) * 19
    blocks_offset = index_offset + index_length

    # Bbox
    all_lats, all_lons = [], []
    for cell in sorted_cells:
        lat, lon = h3.cell_to_latlng(hex(cell)[2:])
        all_lats.append(lat)
        all_lons.append(lon)

    # Write file
    out_path = OUTPUT_DIR / f"{state.abbr}.buildings_v9.ptiles"
    with open(out_path, "wb") as f:
        write_header(
            f,
            MAGIC,
            VERSION,
            min(all_lats),
            min(all_lons),
            max(all_lats),
            max(all_lons),
            total_features,
            len(compressed),
            dict_offset,
            dict_length,
            index_offset,
            index_length,
            blocks_offset,
        )
        # Write dict at dict_offset (already skipped by header)
        f.seek(dict_offset)
        f.write(dict_data)
        # Write index
        f.seek(index_offset)
        write_index(f, index_entries)
        # Write compressed blocks
        f.seek(blocks_offset)
        for cell in sorted_cells:
            f.write(compressed[cell])
    stamp_boundary(out_path, state)

    dt = time.time() - t0
    sz = out_path.stat().st_size
    print(f"  Wrote {sz:,} bytes in {dt:.1f}s", flush=True)
    return {
        "abbr": state.abbr,
        "buildings": total_features,
        "cells": len(cells),
        "bytes": sz,
        "time_s": round(dt, 1),
    }


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("target", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument(
        "--countries",
        help="Comma-separated countries, e.g. JP. Buildings do not fit one file "
             "at country scale, so this expands to the subdivisions where a "
             "country declares them (JP -> its 8 regions).",
    )
    args = p.parse_args()

    targets = []
    if args.all:
        targets = [s for s in STATES if find_pbf(s, prefer=PBF_DIR)]
    elif args.countries:
        for c in args.countries.split(","):
            # subdivisions=True: 29.5M Japanese buildings will not fit a single
            # build, so the regional scopes are the ones that can be built.
            found = scopes_for_country(c.strip(), subdivisions=True)
            if not found:
                print(f"No declared scopes for country: {c.strip()}")
            targets.extend(s for s in (get_state(a) for a in found) if s)
    elif args.target:
        s = get_state(args.target)
        if s:
            targets = [s]
        else:
            print(f"Unknown: {args.target}")
            return
    else:
        p.print_help()
        return

    results = []
    for s in targets:
        out_path = OUTPUT_DIR / f"{s.abbr}.buildings_v9.ptiles"
        if out_path.exists():
            print(f"  SKIP {s.abbr} — already exists", flush=True)
            continue
        try:
            r = build_state_pbf(s)
            if r:
                results.append(r)
        except Exception as e:
            print(f"ERROR {s.abbr}: {e}", flush=True)
            import traceback

            traceback.print_exc()

    if results:
        print("\n=== SUMMARY ===")
        for r in results:
            print(
                f"  {r['abbr']:2s} {r['buildings']:8d} bldgs  {r.get('cells', 0):4d} cells  {r.get('bytes', 0):10,d} B  {r.get('time_s', 0):6.1f}s"
            )


if __name__ == "__main__":
    main()
