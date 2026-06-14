#!/usr/bin/env python3
"""
Generate PTILES v8 per-state files from Overture PMTiles.

This is a thin wrapper around the HTTP extraction + v8 encoding pipeline.
It produces one .ptiles file per state in data/states/<ABBR>.buildings_v8.ptiles.

Requires:
  - pmtiles serve running on localhost:8099 (Go pmtiles binary)
  - Source: /home/aoi/data/protomaps/20260513.pmtiles mounted at that server

Usage:
  python generate_state_v8.py TN
  python generate_state_v8.py --all
"""

import sys
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

import io
import time
import math
import urllib.request
from pathlib import Path
from collections import defaultdict

import h3
import mapbox_vector_tile

from shared import (
    HEADER_SIZE,
    write_header as _write_header,
    write_index as _write_index,
    encode_index_entry,
)
from encode_v8 import encode_block_v8
from states import STATES, get_state, state_bbox

PMTILES_URL = "http://localhost:8099"
OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
EXTRACT_ZOOM = 14
H3_RES = 7
MAGIC = b"PTILESF\x00"
VERSION = 8


def lonlat_to_tile(lon, lat, z):
    n = 2.0 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def tile_bounds(z, x, y):
    n = 2.0 ** z
    min_lon = x / n * 360.0 - 180.0
    max_lon = (x + 1) / n * 360.0 - 180.0
    min_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    max_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return min_lon, min_lat, max_lon, max_lat


def tiles_for_bbox(min_lon, min_lat, max_lon, max_lat, z):
    x0, y0 = lonlat_to_tile(min_lon, max_lat, z)
    x1, y1 = lonlat_to_tile(max_lon, min_lat, z)
    tiles = []
    for x in range(max(0, x0 - 1), min(2**z, x1 + 2)):
        for y in range(max(0, y0 - 1), min(2**z, y1 + 2)):
            tminx, tminy, tmaxx, tmaxy = tile_bounds(z, x, y)
            if not (tmaxx < min_lon or tminx > max_lon or tmaxy < min_lat or tminy > max_lat):
                tiles.append((z, x, y))
    return tiles


def fetch_tile(z, x, y, retries=3):
    url = f"{PMTILES_URL}/{z}/{x}/{y}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ptiles-state/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read() or None
        except urllib.error.HTTPError as e:
            if e.code in (404, 204):
                return None
            if attempt == retries - 1:
                return None
            time.sleep(0.5)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(0.5)
    return None


def extract_buildings(tile_data):
    if not tile_data:
        return []
    try:
        decoded = mapbox_vector_tile.decode(tile_data)
    except Exception:
        return []
    out = []
    for layer_name, layer in decoded.items():
        if "building" not in layer_name.lower():
            continue
        for feat in layer.get("features", []):
            g = feat.get("geometry")
            if not g or g.get("type") != "Polygon":
                continue
            ring = g.get("coordinates", [[]])[0]
            if len(ring) < 4:
                continue
            coords = [(float(p[0]), float(p[1])) for p in ring]
            props = feat.get("properties", {})
            out.append({
                "osm_id": props.get("id") or props.get("osm_id") or 0,
                "coords": coords,
                "building_type": props.get("building") or props.get("building:use") or "yes",
                "name": props.get("name"),
                "height_m": props.get("height"),
            })
    return out


def group_by_h3(buildings):
    cells = defaultdict(list)
    for b in buildings:
        if not b.get("coords"):
            continue
        lon, lat = b["coords"][0]
        try:
            cell = h3.latlng_to_cell(lat, lon, H3_RES)
            cells[cell].append(b)
        except Exception:
            continue
    return cells


def write_ptiles_file(path: Path, buildings_by_cell: dict, state_bbox: tuple):
    """Stream v8 blocks to disk with proper header + index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    min_lon, min_lat, max_lon, max_lat = state_bbox

    with path.open("wb") as f:
        # Placeholder header (will be overwritten at end)
        f.write(b"\x00" * HEADER_SIZE)

        index_entries = []
        block_offset = HEADER_SIZE
        total_features = 0

        for cell, blist in sorted(buildings_by_cell.items()):
            # encode_block_v8 returns (block_bytes, feature_count)
            block_bytes, feat_count = encode_block_v8(blist, int(cell, 16))
            if not block_bytes:
                continue
            f.write(block_bytes)
            index_entries.append({
                "h3_cell": int(cell, 16),
                "block_offset": block_offset,
                "block_length": len(block_bytes),
                "feature_count": feat_count,
            })
            block_offset += len(block_bytes)
            total_features += feat_count

        # Now write the real header at offset 0
        dict_offset = 0
        dict_length = 0
        index_offset = block_offset
        index_length = 4 + len(index_entries) * 20  # rough
        blocks_offset = HEADER_SIZE

        _write_header(
            f, MAGIC, VERSION,
            min_lat, min_lon, max_lat, max_lon,
            total_features, len(index_entries),
            dict_offset, dict_length,
            index_offset, index_length,
            blocks_offset,
        )

        # Seek back and write index after header
        f.seek(HEADER_SIZE)
        _write_index(f, index_entries)

    return path.stat().st_size


def build_state(state):
    print(f"\n=== {state.abbr} {state.name} ===")
    t0 = time.time()
    bbox = state_bbox(state)
    tiles = tiles_for_bbox(*bbox, EXTRACT_ZOOM)
    print(f"  tiles: {len(tiles)}")

    per_cell = defaultdict(list)
    total = 0
    for i, (z, x, y) in enumerate(tiles):
        data = fetch_tile(z, x, y)
        if data:
            bldgs = extract_buildings(data)
            grouped = group_by_h3(bldgs)
            for c, lst in grouped.items():
                per_cell[c].extend(lst)
            total += len(bldgs)
        if (i + 1) % 200 == 0:
            print(f"    {i+1}/{len(tiles)} tiles, {total} buildings")

    if total == 0:
        print("  no buildings — skipping")
        return {"abbr": state.abbr, "buildings": 0, "bytes": 0}

    size = write_ptiles_file(OUTPUT_DIR / f"{state.abbr}.buildings_v8.ptiles", per_cell, bbox)
    dt = time.time() - t0
    print(f"  wrote {size:,} bytes in {dt:.1f}s")
    return {"abbr": state.abbr, "buildings": total, "bytes": size, "time_s": round(dt, 1)}


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("target", nargs="?", default=None)
    p.add_argument("--all", action="store_true")
    args = p.parse_args()

    if args.all or (args.target and args.target.lower() in ("--all", "all")):
        results = []
        for s in STATES:
            try:
                r = build_state(s)
                results.append(r)
            except Exception as e:
                print(f"ERROR {s.abbr}: {e}")
        print("\n=== SUMMARY ===")
        for r in results:
            if r.get("buildings", 0) == 0:
                print(f"  {r['abbr']:2s} skipped")
            else:
                print(f"  {r['abbr']:2s} {r['buildings']:7d} bldgs  {r['bytes']:10,d} B  {r.get('time_s', 0):6.1f}s")
        return

    if not args.target:
        p.print_help()
        return

    s = get_state(args.target)
    if not s:
        print(f"Unknown: {args.target}")
        return
    build_state(s)


if __name__ == "__main__":
    main()
