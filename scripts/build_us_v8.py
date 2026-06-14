#!/usr/bin/env python3
"""
PMTiles → PTILES v8: US building footprints extraction.

Reads Overture buildings from PMTiles (zstd-compressed MVT tiles at z14),
extracts building polygon features, assigns to H3 res-7 cells, encodes
with v8 packing, and writes US.buildings_v8.ptiles.

Two-pass approach (memory-safe for 77M buildings):
  Pass 1: Iterate all PMTiles entries, decode MVT, extract buildings,
           encode per-cell blocks, save to per-cell temp files.
  Pass 2: Merge temp files → train zstd dict → compress → write final file.

Source: /home/aoi/data/protomaps/20260513.pmtiles (23 GB, Overture 2026-05-13)
Output: /home/aoi/kino/projects/ptiles/data/US.buildings_v8.ptiles (~1.4 GB)
Temp:   /tmp/ptiles_us_v8/ (~5 GB, cleaned up after)
"""

import sys
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

import os
import struct
import time
import json
import math
import gc
import shutil
import hashlib
from collections import defaultdict
from pathlib import Path

from pmtiles.reader import Reader, MmapSource
import mapbox_vector_tile
import h3

from shared import (
    write_header, HEADER_SIZE,
    write_index,
    train_dictionary, compress_block,
)
from encode_v8 import (
    encode_building_v8, encode_block_v8,
    build_string_table, encode_string_table,
    classify_height_tier, classify_use,
)

# --- Config ---

PMTILES_PATH = "/home/aoi/data/protomaps/20260513.pmtiles"
OUTPUT_PATH = "/home/aoi/kino/projects/ptiles/data/US.buildings_v8.ptiles"
TEMP_DIR = "/tmp/ptiles_us_v8"
EXTRACT_ZOOM = 14  # Overture buildings at z14
H3_RES = 7
MAGIC = b"PTILESF\x00"
VERSION = 8

# US bbox for filtering
US_BBOX = {
    "min_lon": -125.0, "max_lon": -66.0,
    "min_lat": 24.0, "max_lat": 50.0,
}

# --- Tile → lon/lat helpers ---

def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (min_lon, min_lat, max_lon, max_lat) for tile."""
    n = 2.0 ** z
    min_lon = x / n * 360.0 - 180.0
    max_lon = (x + 1) / n * 360.0 - 180.0
    min_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    max_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return min_lon, min_lat, max_lon, max_lat


def tile_overlaps_us(z: int, x: int, y: int) -> bool:
    """Check if tile bbox overlaps US bbox."""
    t_min_lon, t_min_lat, t_max_lon, t_max_lat = tile_bounds(z, x, y)
    return not (t_max_lon < US_BBOX["min_lon"] or t_min_lon > US_BBOX["max_lon"] or
                t_max_lat < US_BBOX["min_lat"] or t_min_lat > US_BBOX["max_lat"])


def tile_center(z: int, x: int, y: int) -> tuple[float, float]:
    """Return (lon, lat) of tile center."""
    t_min_lon, t_min_lat, t_max_lon, t_max_lat = tile_bounds(z, x, y)
    return ((t_min_lon + t_max_lon) / 2, (t_min_lat + t_max_lat) / 2)


# --- MVT → building dicts ---

def extract_buildings_from_tile(tile_data: bytes, z: int, x: int, y: int) -> list[dict]:
    """Decode MVT tile and extract building features."""
    if not tile_data:
        return []

    try:
        result = mapbox_vector_tile.decode(tile_data)
    except Exception:
        return []

    buildings = []
    tile_center_lon, tile_center_lat = tile_center(z, x, y)

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

            # Convert MVT tile coords to lon/lat
            # MVT uses tile-local coords (0-4096), need to convert to lon/lat
            world_coords = []
            for ring in (coords if geom_type == "Polygon" else coords[0]):
                ring_coords = []
                for pt in ring:
                    lon = tile_center_lon + (pt[0] / 4096.0 - 0.5) * (360.0 / (2**z))
                    lat = tile_center_lat + (0.5 - pt[1] / 4096.0) * (360.0 / (2**z))
                    ring_coords.append([lon, lat])
                world_coords.append(ring_coords)

            if not world_coords or not world_coords[0]:
                continue

            # Simplify by taking outer ring only
            outer_ring = world_coords[0]
            if len(outer_ring) < 4:
                continue

            # Close ring if needed
            if outer_ring[0] != outer_ring[-1]:
                outer_ring.append(outer_ring[0])

            btype = props.get("building", "yes")
            height = props.get("height")
            if height:
                try:
                    height = float(height)
                except (ValueError, TypeError):
                    height = None

            building = {
                "osm_id": props.get("@id", 0) or hash(f"{z}/{x}/{y}/{len(buildings)}"),
                "coords": outer_ring,
                "building_type": btype,
                "height_m": height,
            }

            name = props.get("name")
            if name:
                building["name"] = str(name)

            buildings.append(building)

    return buildings


# --- Pass 1: Extract from PMTiles → per-cell temp files ---

def pass1_extract() -> dict[int, str]:
    """Extract all buildings from PMTiles, save per-cell blocks to temp files.

    Returns:
        {cell_id: temp_file_path} for all cells with buildings.
    """
    print("=== Pass 1: Extract from PMTiles ===", flush=True)
    os.makedirs(TEMP_DIR, exist_ok=True)

    # Open PMTiles
    print(f"Opening {PMTILES_PATH}...", flush=True)
    src = MmapSource(open(PMTILES_PATH, "rb"))
    reader = Reader(src)
    header = reader.header()

    total_entries = header["tile_entries_count"]
    print(f"PMTiles: {total_entries} tile entries, zoom {header['min_zoom']}-{header['max_zoom']}",
          flush=True)
    print(f"Bounds: lon {header['min_lon_e7']/1e7:.1f} to {header['max_lon_e7']/1e7:.1f}, "
          f"lat {header['min_lat_e7']/1e7:.1f} to {header['max_lat_e7']/1e7:.1f}",
          flush=True)

    # Accumulate buildings per cell
    cell_buildings: dict[int, list[dict]] = defaultdict(list)
    total_buildings = 0
    tiles_processed = 0
    tiles_with_buildings = 0
    start_time = time.time()

    entries = reader.entries()
    for i, entry in enumerate(entries):
        z, x, y = entry["z"], entry["x"], entry["y"]

        if z != EXTRACT_ZOOM:
            continue

        if not tile_overlaps_us(z, x, y):
            continue

        tiles_processed += 1
        if tiles_processed % 5000 == 0:
            elapsed = time.time() - start_time
            rate = tiles_processed / elapsed if elapsed > 0 else 0
            print(f"  Processed {tiles_processed} US tiles, {total_buildings:,} buildings "
                  f"({rate:.0f} tiles/s, in {len(cell_buildings)} cells)", flush=True)
            # Flush every 10K tiles to manage memory
            if len(cell_buildings) > 50000:
                _flush_cells(cell_buildings)
                gc.collect()

        try:
            tile_data = reader.get(z, x, y)
        except Exception:
            continue

        if not tile_data:
            continue

        buildings = extract_buildings_from_tile(tile_data, z, x, y)
        if not buildings:
            continue

        tiles_with_buildings += 1

        for b in buildings:
            # Compute H3 cell from centroid of coords
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

    # Flush remaining
    _flush_cells(cell_buildings)

    elapsed = time.time() - start_time
    print(f"\nPass 1 complete: {total_buildings:,} buildings in "
          f"{len(os.listdir(TEMP_DIR))} cells after {elapsed:.0f}s",
          flush=True)
    print(f"  Tiles processed: {tiles_processed:,} "
          f"({tiles_with_buildings:,} had buildings)", flush=True)

    # Build cell → temp file mapping
    cell_files = {}
    for fname in os.listdir(TEMP_DIR):
        if fname.endswith(".v8tmp"):
            cell = int(fname.replace(".v8tmp", ""), 16)
            cell_files[cell] = os.path.join(TEMP_DIR, fname)

    return cell_files


def _flush_cells(cell_buildings: dict[int, list[dict]]):
    """Write accumulated buildings to per-cell temp files, clear dict."""
    # Build cell center cache for this batch
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

        # Sort by OSM ID
        buildings.sort(key=lambda b: b.get("osm_id", 0))

        # Encode block
        try:
            block_bytes, count = encode_block_v8(buildings, cell, cell_centers)
        except Exception as e:
            print(f"  WARN: Failed to encode cell {hex(cell)[:12]}: {e}", flush=True)
            continue

        # Append to temp file
        cell_hex = hex(cell)[2:]
        tmp_path = os.path.join(TEMP_DIR, f"{cell_hex}.v8tmp")
        with open(tmp_path, "ab") as f:
            f.write(block_bytes)

    cell_buildings.clear()


# --- Pass 2: Merge → compress → write ---

def pass2_merge_write(cell_files: dict[int, str]):
    """Merge temp files, train dictionary, compress, write final PTILES."""
    print(f"\n=== Pass 2: Merge & Write ===", flush=True)
    print(f"Cells: {len(cell_files)}", flush=True)

    # Load all raw blocks
    raw_blocks: dict[int, bytes] = {}
    total_features = 0
    for cell, path in cell_files.items():
        with open(path, "rb") as f:
            raw_blocks[cell] = f.read()
        # Feature count is approximate at this stage
    total_features = sum(1 for _ in raw_blocks)  # placeholder

    # Train zstd dictionary
    samples = list(raw_blocks.values())[:2000]
    print(f"  Training zstd dictionary on {len(samples)} samples...", flush=True)
    dict_data = train_dictionary(samples)

    # Build cell center cache for bbox
    cell_centers = {}
    for cell in raw_blocks:
        cell_hex = hex(cell)[2:]
        try:
            lat, lon = h3.cell_to_latlng(cell_hex)
            cell_centers[cell] = (lon, lat)
        except Exception:
            continue

    # Compress each block
    print(f"  Compressing {len(raw_blocks)} blocks...", flush=True)
    compressed = {}
    sorted_cells = sorted(raw_blocks.keys())
    for cell in sorted_cells:
        compressed[cell] = compress_block(raw_blocks[cell], dict_data)

    # Calculate offsets
    dict_offset = HEADER_SIZE
    dict_length = len(dict_data)
    index_offset = dict_offset + dict_length
    index_length = 4 + len(sorted_cells) * 19
    blocks_offset = index_offset + index_length

    # Build index entries
    index_entries = []
    running_offset = blocks_offset
    for cell in sorted_cells:
        cb = compressed[cell]
        index_entries.append({
            "h3_cell": cell,
            "block_offset": running_offset,
            "block_length": len(cb),
            "feature_count": 0,  # filled below
        })
        running_offset += len(cb)

    # Bbox from cell centers
    all_lats = [c[1] for c in cell_centers.values()]
    all_lons = [c[0] for c in cell_centers.values()]

    # Count total features by scanning one block per cell
    # (blocks are stored as length-prefixed records, count the prefixes)
    print(f"  Counting features...", flush=True)
    total_features = 0
    for i, cell in enumerate(sorted_cells):
        raw = raw_blocks[cell]
        # Skip string table: first byte is table count, read past it
        if not raw:
            continue
        pos = 1  # skip table count
        table_count = raw[0]
        for _ in range(table_count):
            if pos >= len(raw):
                break
            slen = raw[pos]
            pos += 1 + slen

        # Now count records (each preceded by u32 length)
        cell_features = 0
        while pos + 4 <= len(raw):
            rec_len = struct.unpack_from("<I", raw, pos)[0]
            pos += 4 + rec_len
            cell_features += 1
        index_entries[i]["feature_count"] = min(cell_features, 65535)
        total_features += cell_features

    print(f"  Total features: {total_features:,}", flush=True)

    # Write file
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
    print(f"\n=== Done ===", flush=True)
    print(f"  Output: {OUTPUT_PATH}", flush=True)
    print(f"  Size: {total_size:,} bytes ({total_size/1024/1024:.1f} MB)", flush=True)
    print(f"  Features: {total_features:,}", flush=True)
    print(f"  Cells: {len(compressed):,}", flush=True)
    print(f"  B/bldg: {total_size/total_features:.1f}" if total_features > 0 else "", flush=True)


# --- Main ---

def main():
    start = time.time()

    # Storage check
    stat = os.statvfs(os.path.dirname(OUTPUT_PATH) or ".")
    free_gb = (stat.f_frsize * stat.f_bavail) / (1024**3)
    print(f"Free disk: {free_gb:.0f} GB", flush=True)

    if free_gb < 20:
        print("ERROR: Need at least 20 GB free (for temp + output). Aborting.", flush=True)
        sys.exit(1)

    print(f"Temp dir: {TEMP_DIR}", flush=True)
    print(f"Output: {OUTPUT_PATH}", flush=True)

    # Clean temp from previous runs
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)

    try:
        cell_files = pass1_extract()

        if not cell_files:
            print("ERROR: No buildings extracted!", flush=True)
            sys.exit(1)

        pass2_merge_write(cell_files)

    finally:
        # Cleanup temp
        if os.path.exists(TEMP_DIR):
            print(f"\nCleaning up {TEMP_DIR}...", flush=True)
            shutil.rmtree(TEMP_DIR)

    elapsed = time.time() - start
    print(f"\nTotal time: {elapsed:.0f}s ({elapsed/60:.1f}m)", flush=True)


if __name__ == "__main__":
    main()
