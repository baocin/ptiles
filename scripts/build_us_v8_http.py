#!/usr/bin/env python3
"""
build_us_v8_http.py — Query local pmtiles serve via HTTP, extract z14 US buildings.

Requires pmtiles serve running on localhost:8099.
Two-pass: extract per-cell to temp → merge + compress → write PTILES.
"""

import sys
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

import os
import struct
import time
import math
import gc
import shutil
import urllib.request
from collections import defaultdict
from pathlib import Path

import h3
import mapbox_vector_tile

from shared import (
    write_header, HEADER_SIZE,
    write_index,
    train_dictionary, compress_block,
)
from encode_v8 import encode_block_v8

# --- Config ---

PMTILES_URL = "http://localhost:8099"
OUTPUT_PATH = "/home/aoi/kino/projects/ptiles/data/US.buildings_v8.ptiles"
TEMP_DIR = "/tmp/ptiles_us_v8"
EXTRACT_ZOOM = 14
H3_RES = 7
MAGIC = b"PTILESF\x00"
VERSION = 8

US_BBOX = (-125.0, 24.0, -66.0, 50.0)


def lonlat_to_tile(lon, lat, z):
    n = 2.0 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat)) +
            1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def tile_center(z, x, y):
    n = 2.0 ** z
    lon = (x + 0.5) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
    return lon, lat


def fetch_tile(z, x, y, retries=3):
    url = f"{PMTILES_URL}/{z}/{x}/{y}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'ptiles-builder/1.0')
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                if data and len(data) > 0:
                    return data
                return None
        except urllib.error.HTTPError as e:
            if e.code == 404 or e.code == 204:
                return None
            if attempt == retries - 1:
                return None
            time.sleep(0.5)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(0.5)
    return None


def extract_buildings_from_tile(tile_data, z, x, y):
    if not tile_data:
        return []

    try:
        result = mapbox_vector_tile.decode(tile_data)
    except Exception:
        return []

    buildings = []
    tc_lon, tc_lat = tile_center(z, x, y)
    tile_deg = 360.0 / (2 ** z)

    for layer_name, layer in result.items():
        if layer_name != "building":
            continue

        for feature in layer.get("features", []):
            geom_type = feature.get("geometry", {}).get("type", "")
            coords = feature.get("geometry", {}).get("coordinates", [])

            if geom_type not in ("Polygon", "MultiPolygon"):
                continue

            props = feature.get("properties", {})
            if not props.get("building"):
                continue

            rings = coords if geom_type == "Polygon" else coords[0]
            if not rings or len(rings) < 4:
                continue

            # Convert MVT pixel coords (0-4095) to lon/lat
            outer = []
            for pt in rings:
                lon = tc_lon + (pt[0] / 4096.0 - 0.5) * tile_deg
                lat = tc_lat + (0.5 - pt[1] / 4096.0) * tile_deg
                outer.append([lon, lat])

            if outer[0] != outer[-1]:
                outer.append(outer[0])

            btype = str(props.get("building", "yes"))
            height = props.get("height")
            if height:
                try:
                    height = float(height)
                except (ValueError, TypeError):
                    height = None

            building = {
                "osm_id": props.get("@id", 0) or abs(hash(f"{z}/{x}/{y}/{len(buildings)}")) % (10**10),
                "coords": outer,
                "building_type": btype,
                "height_m": height,
            }

            name = props.get("name")
            if name:
                building["name"] = str(name)

            buildings.append(building)

    return buildings


def pass1_extract():
    print("=== Pass 1: HTTP Tile Extraction ===", flush=True)
    os.makedirs(TEMP_DIR, exist_ok=True)

    min_lon, min_lat, max_lon, max_lat = US_BBOX
    x1, y1 = lonlat_to_tile(min_lon, max_lat, EXTRACT_ZOOM)
    x2, y2 = lonlat_to_tile(max_lon, min_lat, EXTRACT_ZOOM)

    total_tiles = (x2 - x1 + 1) * (y2 - y1 + 1)
    print(f"US tiles at z{EXTRACT_ZOOM}: x=[{x1},{x2}] y=[{y1},{y2}] = {total_tiles:,}",
          flush=True)

    cell_buildings: dict[int, list[dict]] = defaultdict(list)
    total_buildings = 0
    tiles_fetched = 0
    tiles_with_data = 0
    start_time = time.time()
    flush_interval = 50000

    for y in range(y1, y2 + 1):
        for x in range(x1, x2 + 1):
            tiles_fetched += 1

            if tiles_fetched % 5000 == 0:
                elapsed = time.time() - start_time
                rate = tiles_fetched / elapsed if elapsed > 0 else 0
                print(f"  {tiles_fetched}/{total_tiles} tiles, "
                      f"{total_buildings:,} buildings, {rate:.0f} t/s, "
                      f"{len(cell_buildings)} cells", flush=True)

            if tiles_fetched % flush_interval == 0 and len(cell_buildings) > 30000:
                _flush_cells(cell_buildings)
                gc.collect()

            tile_data = fetch_tile(EXTRACT_ZOOM, x, y)
            if not tile_data:
                continue

            buildings = extract_buildings_from_tile(tile_data, EXTRACT_ZOOM, x, y)
            if not buildings:
                continue

            tiles_with_data += 1

            for b in buildings:
                coords = b["coords"]
                lats = [c[1] for c in coords]
                lons = [c[0] for c in coords]
                lat = sum(lats) / len(lats)
                lon = sum(lons) / len(lons)

                cell_hex = h3.latlng_to_cell(lat, lon, H3_RES)
                if isinstance(cell_hex, str):
                    cell = int(cell_hex, 16)
                else:
                    cell = int(cell_hex)

                cell_buildings[cell].append(b)
                total_buildings += 1

    _flush_cells(cell_buildings)

    elapsed = time.time() - start_time
    cell_count = len([f for f in os.listdir(TEMP_DIR) if f.endswith(".v8tmp")])
    print(f"\nPass 1 complete: {total_buildings:,} buildings in {cell_count} cells "
          f"after {elapsed:.0f}s ({elapsed/60:.1f}m)", flush=True)
    print(f"  Tiles: {tiles_fetched:,} fetched, {tiles_with_data:,} with data "
          f"({100*tiles_with_data/max(1,tiles_fetched):.1f}%)", flush=True)

    return {int(f.replace(".v8tmp", ""), 16): os.path.join(TEMP_DIR, f)
            for f in os.listdir(TEMP_DIR) if f.endswith(".v8tmp")}


def _flush_cells(cell_buildings):
    cell_centers = {}
    for cell in cell_buildings:
        cell_hex = hex(cell)[2:]
        try:
            lat, lon = h3.cell_to_latlng(cell_hex)
            cell_centers[cell] = (lon, lat)
        except Exception:
            continue

    for cell, buildings in cell_buildings.items():
        if cell not in cell_centers:
            continue

        buildings.sort(key=lambda b: b.get("osm_id", 0))
        try:
            block_bytes, count = encode_block_v8(buildings, cell, cell_centers)
        except Exception as e:
            print(f"  WARN: encode cell fail: {e}", flush=True)
            continue

        cell_hex = hex(cell)[2:]
        tmp_path = os.path.join(TEMP_DIR, f"{cell_hex}.v8tmp")
        with open(tmp_path, "ab") as f:
            f.write(block_bytes)

    cell_buildings.clear()


def pass2_merge_write(cell_files):
    print(f"\n=== Pass 2: Merge & Write ===", flush=True)
    print(f"Cells: {len(cell_files)}", flush=True)

    raw_blocks = {}
    for cell, path in cell_files.items():
        with open(path, "rb") as f:
            raw_blocks[cell] = f.read()

    samples = list(raw_blocks.values())[:2000]
    print(f"  Training zstd dictionary on {len(samples)} samples...", flush=True)
    dict_data = train_dictionary(samples)

    cell_centers = {}
    for cell in raw_blocks:
        cell_hex = hex(cell)[2:]
        try:
            lat, lon = h3.cell_to_latlng(cell_hex)
            cell_centers[cell] = (lon, lat)
        except Exception:
            continue

    print(f"  Compressing {len(raw_blocks)} blocks...", flush=True)
    compressed = {}
    sorted_cells = sorted(raw_blocks.keys())
    for cell in sorted_cells:
        compressed[cell] = compress_block(raw_blocks[cell], dict_data)

    dict_offset = HEADER_SIZE
    dict_length = len(dict_data)
    index_offset = dict_offset + dict_length
    index_length = 4 + len(sorted_cells) * 19
    blocks_offset = index_offset + index_length

    index_entries = []
    running_offset = blocks_offset
    total_features = 0
    for cell in sorted_cells:
        cb = compressed[cell]
        raw = raw_blocks[cell]
        # Count features: skip string table, count u32 length prefixes
        if raw:
            pos = 1
            table_count = raw[0]
            for _ in range(table_count):
                if pos >= len(raw):
                    break
                slen = raw[pos]
                pos += 1 + slen
            cell_features = 0
            while pos + 4 <= len(raw):
                rec_len = struct.unpack_from("<I", raw, pos)[0]
                pos += 4 + rec_len
                cell_features += 1
        else:
            cell_features = 0

        index_entries.append({
            "h3_cell": cell,
            "block_offset": running_offset,
            "block_length": len(cb),
            "feature_count": min(cell_features, 65535),
        })
        running_offset += len(cb)
        total_features += cell_features

    all_lats = [c[1] for c in cell_centers.values()]
    all_lons = [c[0] for c in cell_centers.values()]

    print(f"  Writing {OUTPUT_PATH}...", flush=True)
    with open(OUTPUT_PATH, "wb") as f:
        write_header(
            f, MAGIC, VERSION,
            min(all_lats), min(all_lons), max(all_lats), max(all_lons),
            total_features, len(compressed),
            dict_offset, dict_length,
            index_offset, index_length,
            blocks_offset,
        )
        f.write(dict_data)
        write_index(f, index_entries)
        for cell in sorted_cells:
            f.write(compressed[cell])

    total_size = os.path.getsize(OUTPUT_PATH)
    print(f"\nPTILES v8 written: {OUTPUT_PATH}", flush=True)
    print(f"  Size: {total_size:,} bytes ({total_size/1024/1024:.1f} MB)", flush=True)
    print(f"  Features: {total_features:,}", flush=True)
    print(f"  Cells: {len(compressed):,}", flush=True)
    if total_features > 0:
        print(f"  B/bldg: {total_size/total_features:.1f}", flush=True)


def main():
    start = time.time()
    stat = os.statvfs(os.path.dirname(OUTPUT_PATH) or ".")
    free_gb = (stat.f_frsize * stat.f_bavail) / (1024 ** 3)
    print(f"Free disk: {free_gb:.0f} GB", flush=True)
    if free_gb < 10:
        print("ERROR: Need 10+ GB free. Aborting.", flush=True)
        sys.exit(1)

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)

    try:
        # Verify server is reachable using a z14 tile (z0 may not exist)
        print("Testing server connectivity (z14 tile)...", flush=True)
        test = fetch_tile(14, 4372, 6427)  # Nashville area
        if test is None:
            # Try alternate tile
            test = fetch_tile(14, 0, 0)
        if test is None:
            print("ERROR: pmtiles serve not reachable. Is it running on :8099?", flush=True)
            sys.exit(1)
        print(f"Server reachable (test tile: {len(test) if test else 0} bytes)", flush=True)

        cell_files = pass1_extract()
        if not cell_files:
            print("ERROR: No buildings extracted!", flush=True)
            sys.exit(1)

        pass2_merge_write(cell_files)
    finally:
        if os.path.exists(TEMP_DIR):
            print(f"Cleaning up {TEMP_DIR}...", flush=True)
            shutil.rmtree(TEMP_DIR)

    elapsed = time.time() - start
    print(f"\nTotal time: {elapsed:.0f}s ({elapsed/60:.1f}m)", flush=True)


if __name__ == "__main__":
    main()
