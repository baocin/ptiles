#!/usr/bin/env python3
"""
Extract building footprints from OSM PBF per US state, encode as PTILES v8.

Usage:
  python extract_buildings.py --states TN,CA     # Specific states
  python extract_buildings.py --all              # All 50+DC
  python extract_buildings.py --list-states      # List available

Source: /home/aoi/data/ptiles-source/north-america-latest.osm.pbf (19 GB)

Strategy: single pass through the PBF, filter by state polygon, group by H3 cell,
encode v8 blocks, write per-state PTILES files.
"""

import sys
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

import json
import math
import time
import os
from pathlib import Path
from collections import defaultdict

import osmium
import h3

from encode_v8 import encode_block_v8
from shared import HEADER_SIZE, write_header, write_index
from states import STATES, get_state, state_bbox, State

# --- Config ---
OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
PBF_PATH = "/home/aoi/data/ptiles-source/north-america-latest.osm.pbf"
WORKER_POOL = os.cpu_count() or 8

MAGIC = b"PTILESF\x00"
VERSION = 8
H3_RES = 7


class BuildingHandler(osmium.SimpleHandler):
    """Parse OSM PBF and collect building nodes/ways/relations."""

    def __init__(self, state_bbox: tuple[float, float, float, float]):
        super().__init__()
        self.min_lon, self.min_lat, self.max_lon, self.max_lat = state_bbox
        self.buildings: list[dict] = []

    def way(self, w):
        """Process a way (most building footprints are ways)."""
        if not any(tag.k == 'building' for tag in w.tags if tag.v):
            return

        # Check if the way's bbox overlaps our state bbox
        if w.nodes:
            c = None
            loc_ok = False
            try:
                c = w.nodes[0]
                _ = c.lon
                loc_ok = True
            except Exception:
                pass
            if not loc_ok or not (self.min_lon <= c.lon <= self.max_lon and
                    self.min_lat <= c.lat <= self.max_lat):
                return

        # Extract coordinates
        coords = []
        for n in w.nodes:
            coords.append((n.lon, n.lat))
        if len(coords) < 4:
            return

        # Get building type
        btype = "yes"
        height_m = None
        name = ""
        for tag in w.tags:
            if tag.k == 'building':
                btype = tag.v
            elif tag.k == 'height':
                try:
                    height_m = float(tag.v.rstrip('m '))
                except (ValueError, AttributeError):
                    pass
            elif tag.k == 'name':
                name = tag.v

        osm_id = w.id
        # Use centroid of first vertex for H3 assignment
        lon, lat = coords[0]
        try:
            cell = h3.latlng_to_cell(lat, lon, H3_RES)
        except Exception:
            return

        self.buildings.append({
            "osm_id": osm_id,
            "coords": coords,
            "building_type": btype,
            "name": name,
            "height_m": height_m,
        })

    def relation(self, r):
        """Skip relations for now - most buildings are ways."""
        pass


def extract_state_buildings(state: State) -> list[dict]:
    """Extract building footprints for a single state from OSM PBF."""
    print(f"  Extracting from PBF...")
    handler = BuildingHandler(state_bbox(state))
    handler.apply_file(PBF_PATH)

    # Also apply file with locations=True for way nodes
    handler2 = BuildingHandler(state_bbox(state))
    handler2.apply_file(PBF_PATH, locations=True)

    buildings = handler2.buildings
    print(f"  Extracted {len(buildings)} buildings")

    # Group by H3 cell
    per_cell = defaultdict(list)
    for b in buildings:
        lon, lat = b["coords"][0]
        try:
            cell = h3.latlng_to_cell(lat, lon, H3_RES)
            per_cell[cell].append(b)
        except Exception:
            continue

    return dict(per_cell)


def write_ptiles_file(path: Path, buildings_by_cell: dict,
                      min_lon: float, min_lat: float,
                      max_lon: float, max_lat: float,
                      compression_level: int = 3) -> int:
    """Write a v8 PTILES file from per-cell building data."""
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("wb") as f:
        # Placeholder header
        f.write(b"\x00" * HEADER_SIZE)

        index_entries = []
        block_offset = HEADER_SIZE
        total_features = 0

        for cell_hex in sorted(buildings_by_cell.keys()):
            blist = buildings_by_cell[cell_hex]
            cell_int = int(cell_hex, 16)
            block_bytes, feat_count = encode_block_v8(blist, cell_hex, compression_level)
            if not block_bytes:
                continue
            f.write(block_bytes)
            index_entries.append({
                "h3_cell": cell_int,
                "block_offset": block_offset,
                "block_length": len(block_bytes),
                "feature_count": feat_count,
            })
            block_offset += len(block_bytes)
            total_features += feat_count

        # Write real header
        dict_offset = 0
        dict_length = 0
        index_offset = block_offset
        index_length = 4 + len(index_entries) * 20  # approx
        blocks_offset = HEADER_SIZE

        write_header(
            f, MAGIC, VERSION,
            min_lat, min_lon, max_lat, max_lon,
            total_features, len(index_entries),
            dict_offset, dict_length,
            index_offset, index_length,
            blocks_offset,
        )

        # Write index after header
        f.seek(HEADER_SIZE)
        write_index(f, index_entries)

    return path.stat().st_size


def build_state(state: State, dry_run: bool = False):
    """Build v8 PTILES for one state from OSM PBF."""
    print(f"\n=== {state.abbr} {state.name} (FIPS {state.fips}) ===")
    t0 = time.time()

    per_cell = extract_state_buildings(state)
    if not per_cell:
        print("  No buildings found")
        return {"abbr": state.abbr, "buildings": 0}

    total_buildings = sum(len(v) for v in per_cell.values())
    print(f"  Grouped into {len(per_cell)} H3 cells")

    if dry_run:
        print(f"  [dry-run] would write {total_buildings} buildings")
        return {"abbr": state.abbr, "buildings": total_buildings, "cells": len(per_cell)}

    bbox = state_bbox(state)
    size = write_ptiles_file(
        OUTPUT_DIR / f"{state.abbr}.buildings_v8.ptiles",
        per_cell, *bbox
    )

    dt = time.time() - t0
    print(f"  Wrote {size:,} bytes in {dt:.1f}s")
    return {
        "abbr": state.abbr,
        "buildings": total_buildings,
        "cells": len(per_cell),
        "bytes": size,
        "time_s": round(dt, 1),
    }


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--states", help="Comma-separated state abbreviations (e.g. TN,CA)")
    p.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--list-states", action="store_true")
    args = p.parse_args()

    if args.list_states:
        for s in STATES:
            print(f"{s.fips} {s.abbr:2s} {s.name:20s}")
        return

    targets = []
    if args.all:
        targets = STATES
    elif args.states:
        for abbr in args.states.split(","):
            s = get_state(abbr.strip())
            if not s:
                print(f"Unknown state: {abbr}")
                continue
            targets.append(s)

    if not targets:
        p.print_help()
        return

    results = []
    for s in targets:
        try:
            r = build_state(s, dry_run=args.dry_run)
            results.append(r)
        except Exception as e:
            print(f"ERROR on {s.abbr}: {e}")
            import traceback
            traceback.print_exc()

    if results:
        print("\n=== SUMMARY ===")
        for r in results:
            if r.get("buildings", 0) == 0:
                print(f"  {r['abbr']:2s} no buildings")
            else:
                b = r.get("bytes", 0)
                print(f"  {r['abbr']:2s} {r['buildings']:7d} bldgs  {r['cells']:4d} cells  {b:10,d} B  {r.get('time_s', 0):6.1f}s")


if __name__ == "__main__":
    main()
