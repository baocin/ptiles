#!/usr/bin/env python3
"""
Build per-state PTILES v8 buildings from per-state OSM PBF.

Usage:
    python build_state_v8.py TN
    python build_state_v8.py --all
"""
import sys, os, struct, time, gc, json
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
import osmium
import h3
import zstandard as zstd
import numpy as np

from shared import (
    write_header, HEADER_SIZE, write_index,
    train_dictionary, compress_block,
    encode_string_table, encode_table_ref,
)
from encode_v8 import (
    encode_building_v8, encode_block_v8,
    classify_height_tier, classify_use,
)
from states import STATES, get_state, state_bbox

PBF_DIR = Path("/home/aoi/kino/projects/ptiles/data/pbfs")
OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
H3_RES = 7
MAGIC = b"PTILESF\x00"
VERSION = 8

STATE_PBF_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut",
    "DE": "delaware", "DC": "district-of-columbia", "FL": "florida",
    "GA": "georgia", "HI": "hawaii", "ID": "idaho", "IL": "illinois",
    "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky",
    "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
    "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri",
    "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new-hampshire",
    "NJ": "new-jersey", "NM": "new-mexico", "NY": "new-york",
    "NC": "north-carolina", "ND": "north-dakota", "OH": "ohio",
    "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode-island", "SC": "south-carolina", "SD": "south-dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
    "VA": "virginia", "WA": "washington", "WV": "west-virginia",
    "WI": "wisconsin", "WY": "wyoming",
}

class BuildingHandler(osmium.SimpleHandler):
    def __init__(self, state_bbox):
        super().__init__()
        self.min_lon, self.min_lat, self.max_lon, self.max_lat = state_bbox
        self.buildings = []

    def way(self, w):
        if not any(tag.k == 'building' for tag in w.tags if tag.v):
            return
        if not w.nodes:
            return
        try:
            lon_sum, lat_sum = 0.0, 0.0
            ring = []
            for node in w.nodes:
                try:
                    lat = node.location.lat
                    lon = node.location.lon
                except Exception:
                    continue
                if not ring:
                    if not (self.min_lon <= lon <= self.max_lon and
                            self.min_lat <= lat <= self.max_lat):
                        return
                ring.append([lon, lat])
                lon_sum += lon
                lat_sum += lat
            if len(ring) < 4:
                return
            if ring[0] != ring[-1]:
                ring.append(ring[0])

            btype = "yes"
            name = None
            height = None
            for tag in w.tags:
                if tag.k == 'building' and tag.v:
                    btype = tag.v
                elif tag.k == 'name':
                    name = tag.v
                elif tag.k == 'height':
                    try:
                        height = float(tag.v) if tag.v else None
                    except ValueError:
                        height = None

            self.buildings.append({
                "osm_id": w.id,
                "coords": ring,
                "building_type": btype,
                "height_m": height,
            })
            if name:
                self.buildings[-1]["name"] = name
        except Exception:
            pass

def build_state_pbf(state):
    print(f"\n=== {state.abbr} {state.name} ===", flush=True)
    t0 = time.time()

    pbf_name = STATE_PBF_NAMES.get(state.abbr)
    if not pbf_name:
        print(f"  No PBF file mapping for {state.abbr}")
        return
    pbf_path = PBF_DIR / f"{pbf_name}-latest.osm.pbf"
    if not pbf_path.exists():
        print(f"  PBF not found: {pbf_path}")
        return

    bbox = state_bbox(state)
    handler = BuildingHandler(bbox)
    handler.apply_file(str(pbf_path), locations=True)

    bldgs = handler.buildings
    if not bldgs:
        print("  No buildings found", flush=True)
        return

    print(f"  Extracted {len(bldgs)} buildings", flush=True)
    bldgs.sort(key=lambda b: b["osm_id"])

    # Group by H3 cell
    cells = defaultdict(list)
    for b in bldgs:
        lon, lat = b["coords"][0]
        cell = h3.latlng_to_cell(lat, lon, H3_RES)
        cells[int(cell, 16)].append(b)
    print(f"  Grouped into {len(cells)} H3 cells", flush=True)

    # Encode blocks
    sorted_cells = sorted(cells.keys())
    raw_blocks = {}
    total_features = 0
    index_entries = []
    for cell in sorted_cells:
        block_bytes, count = encode_block_v8(cells[cell], cell)
        raw_blocks[cell] = block_bytes
        total_features += count
        # NOTE: index_entries populated after compression (need block sizes)

    print(f"  Encoded {total_features} features in {len(raw_blocks)} blocks", flush=True)

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
        index_entries.append({
            "h3_cell": cell,
            "block_offset": cur_block_off,
            "block_length": blen,
            "feature_count": len(cells[cell]),
        })
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
    out_path = OUTPUT_DIR / f"{state.abbr}.buildings_v8.ptiles"
    with open(out_path, "wb") as f:
        write_header(f, MAGIC, VERSION, min(all_lats), min(all_lons),
                     max(all_lats), max(all_lons), total_features, len(compressed),
                     dict_offset, dict_length, index_offset, index_length, blocks_offset)
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

    dt = time.time() - t0
    sz = out_path.stat().st_size
    print(f"  Wrote {sz:,} bytes in {dt:.1f}s", flush=True)
    return {"abbr": state.abbr, "buildings": total_features, "cells": len(cells), "bytes": sz, "time_s": round(dt, 1)}

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("target", nargs="?")
    p.add_argument("--all", action="store_true")
    args = p.parse_args()

    targets = []
    if args.all:
        targets = [s for s in STATES if s.abbr in STATE_PBF_NAMES]
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
            print(f"  {r['abbr']:2s} {r['buildings']:8d} bldgs  {r.get('cells',0):4d} cells  {r.get('bytes',0):10,d} B  {r.get('time_s',0):6.1f}s")

if __name__ == "__main__":
    main()
