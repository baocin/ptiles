#!/usr/bin/env python3
"""
Build TN.buildings_v8.ptiles — full v8 TN building footprints from GeoJSONL.

Uses v8 packing:
- Per-cell string table
- Cell-relative first vertex (i16 offsets)
- Vertex count bias (4-18 packed into flags, 0x0F sentinel)
- Building use + height tier packed into flags

Input:  ~920K buildings from /home/aoi/tennessee-all-enriched.geojsonl
Output: /home/aoi/kino/projects/ptiles/data/TN.buildings_v8.ptiles
"""

import sys
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

import os
import struct
import json
import time
import gc
from collections import defaultdict
import h3
from shapely.geometry import shape
import numpy as np

from shared import (
    write_header, HEADER_SIZE,
    write_index, read_index,
    compress_block, train_dictionary,
    BTYPE_INDEX, USE_MAP,
    encode_string_table, encode_table_ref,
)
from encode_v8 import (
    encode_building_v8, encode_block_v8,
    decode_building_v8, decode_string_table,
    classify_height_tier, classify_use,
)

# --- Config ---
FOOTPRINTS_FILE = "/home/aoi/tennessee-all-enriched.geojsonl"
OUTPUT_FILE = "/home/aoi/kino/projects/ptiles/data/TN.buildings_v8.ptiles"
H3_RES = 7
MAGIC = b"PTILESF\x00"
VERSION = 8

# --- Step 1: Load buildings ---

def load_buildings(fp_path: str) -> list[dict]:
    """Load buildings from GeoJSONL, extract coords from geometry."""
    print(f"Loading buildings from {fp_path}...", flush=True)
    buildings = []
    total = 0
    invalid = 0

    with open(fp_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            if total % 200000 == 0:
                print(f"  Read {total} lines, {len(buildings)} valid so far...", flush=True)

            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue

            geom = feat.get("geometry")
            if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
                invalid += 1
                continue

            coords = geom["coordinates"][0]  # outer ring

            building = {
                "osm_id": feat.get("osm_id", 0),
                "building_type": feat.get("building_type") or "yes",
                "height_m": feat.get("height_m"),
            }

            # Handle MultiPolygon: take outer ring of first polygon
            if geom["type"] == "MultiPolygon":
                coords = geom["coordinates"][0][0]

            # Flip coord order: GeoJSON is [lon, lat], we want [[lon, lat],...]
            building["coords"] = [[c[0], c[1]] for c in coords]

            # Optional fields
            if feat.get("name"):
                building["name"] = feat["name"]
            if feat.get("category"):
                building["category"] = feat["category"]
            if feat.get("name_source"):
                building["name_source"] = feat["name_source"]
            if feat.get("poi_osm_id"):
                building["poi_osm_id"] = feat["poi_osm_id"]
            if feat.get("centroid_lat"):
                building["centroid_lat"] = feat["centroid_lat"]
                building["centroid_lon"] = feat.get("centroid_lon", 0)

            buildings.append(building)

    print(f"  Loaded {len(buildings)} valid buildings "
          f"({total} total, {invalid} invalid/skipped)", flush=True)
    return buildings


# --- Step 2: Compute H3 cells + get cell centers ---

def assign_cells(buildings: list[dict]) -> dict[int, list[dict]]:
    """Assign each building to H3 cell, return cells dict."""
    print(f"Assigning {len(buildings)} buildings to H3 res {H3_RES} cells...", flush=True)
    cells = defaultdict(list)

    for b in buildings:
        if "centroid_lat" in b:
            lat, lon = b["centroid_lat"], b["centroid_lon"]
        elif b.get("coords"):
            coords = b["coords"]
            lats = [c[1] for c in coords]
            lons = [c[0] for c in coords]
            lat = sum(lats) / len(lats)
            lon = sum(lons) / len(lons)
        else:
            continue

        cell_hex = h3.latlng_to_cell(lat, lon, H3_RES)
        if isinstance(cell_hex, str):
            cell = int(cell_hex, 16)
        else:
            cell = int(cell_hex)
        cells[cell].append(b)

    print(f"  Grouped into {len(cells)} cells", flush=True)
    return dict(cells)


# --- Step 3: Build cell center cache ---

def build_cell_centers(cells: dict[int, list[dict]]) -> dict[int, tuple[float, float]]:
    """Build cache of cell -> (lon, lat) centers."""
    centers = {}
    for cell in cells:
        cell_hex = hex(cell)[2:]
        lat, lon = h3.cell_to_latlng(cell_hex)
        centers[cell] = (lon, lat)
    return centers


# --- Step 4: Encode blocks ---

def encode_all_blocks(cells: dict[int, list[dict]],
                      cell_centers: dict[int, tuple[float, float]]
                      ) -> tuple[dict[int, bytes], int, list[dict]]:
    """Encode all cells as v8 blocks.

    Returns:
        (compressed_blocks, total_features, index_entries)
    """
    print(f"Encoding {len(cells)} cell blocks...", flush=True)

    raw_blocks = {}
    total_features = 0
    index_entries = []
    sorted_cells = sorted(cells.keys())

    for i, cell in enumerate(sorted_cells):
        buildings = cells[cell]
        # Sort by OSM ID for delta encoding
        buildings.sort(key=lambda b: b["osm_id"])

        center = cell_centers[cell]
        block_bytes, count = encode_block_v8(buildings, cell, cell_centers)
        raw_blocks[cell] = block_bytes
        total_features += count

        if i > 0 and i % 1000 == 0:
            print(f"  Encoded {i}/{len(cells)} cells, {total_features} features so far", flush=True)

        index_entries.append({
            "h3_cell": cell,
            "block_offset": 0,  # placeholder
            "block_length": 0,  # placeholder
            "feature_count": count,
        })

    print(f"  Encoded {len(sorted_cells)} cells, {total_features} features total", flush=True)
    return raw_blocks, total_features, index_entries


# --- Step 5: Compress blocks and write file ---

def write_ptiles(output_path: str, raw_blocks: dict[int, bytes],
                 index_entries: list[dict], total_features: int):
    """Compress blocks with zstd dictionary and write PTILES file."""
    print(f"Writing {output_path}...", flush=True)

    # Train zstd dictionary
    samples = list(raw_blocks.values())[:2000]
    print(f"  Training zstd dictionary on {len(samples)} samples...", flush=True)
    dict_data = train_dictionary(samples)

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
    index_length = 4 + len(index_entries) * 19
    blocks_offset = index_offset + index_length

    # Build bbox from index entries
    all_lats = []
    all_lons = []
    for cell in sorted_cells:
        cell_hex = hex(cell)[2:]
        lat, lon = h3.cell_to_latlng(cell_hex)
        all_lats.append(lat)
        all_lons.append(lon)

    # Write file
    with open(output_path, "wb") as f:
        write_header(
            f, MAGIC, VERSION,
            min(all_lats), min(all_lons), max(all_lats), max(all_lons),
            total_features, len(compressed),
            dict_offset, dict_length,
            index_offset, index_length,
            blocks_offset,
        )

        # Dictionary
        f.write(dict_data)

        # Index (with absolute offsets)
        running_offset = blocks_offset
        for i, entry in enumerate(index_entries):
            cell = entry["h3_cell"]
            cb = compressed[cell]
            entry["block_offset"] = running_offset
            entry["block_length"] = len(cb)
            running_offset += len(cb)

        # Re-sort index entries by H3 cell (in case order changed)
        index_entries.sort(key=lambda e: e["h3_cell"])
        write_index(f, index_entries)

        # Blocks
        for entry in index_entries:
            cell = entry["h3_cell"]
            f.write(compressed[cell])

    total_size = os.path.getsize(output_path)
    raw_total = sum(len(raw_blocks[c]) for c in sorted_cells)
    comp_total = total_size - HEADER_SIZE - dict_length - index_length

    print(f"\nPTILES v{VERSION} written: {output_path}", flush=True)
    print(f"  Total size: {total_size:,} bytes ({total_size/1024/1024:.1f} MB)", flush=True)
    print(f"  Features: {total_features:,}", flush=True)
    print(f"  Cells: {len(compressed):,}", flush=True)
    print(f"  Dictionary: {dict_length:,} bytes", flush=True)
    print(f"  Index: {index_length:,} bytes ({len(index_entries)} entries)", flush=True)
    print(f"  Raw blocks: {raw_total:,} bytes ({raw_total/1024/1024:.1f} MB)", flush=True)
    print(f"  Compressed blocks: {comp_total:,} bytes ({comp_total/1024/1024:.1f} MB)", flush=True)
    print(f"  Compression ratio: {raw_total/comp_total:.1f}x" if comp_total > 0 else "", flush=True)
    print(f"  Bytes per building: {total_size/total_features:.1f}", flush=True)


# --- Main ---

def main():
    start = time.time()

    buildings = load_buildings(FOOTPRINTS_FILE)
    if not buildings:
        print("No buildings loaded!", flush=True)
        sys.exit(1)

    elapsed = time.time() - start
    print(f"\nLoading took {elapsed:.0f}s", flush=True)

    # Assign to H3 cells
    cells = assign_cells(buildings)
    cell_centers = build_cell_centers(cells)

    # Free buildings list if memory is tight
    del buildings
    gc.collect()

    # Encode
    encode_start = time.time()
    raw_blocks, total_features, index_entries = encode_all_blocks(cells, cell_centers)
    encode_elapsed = time.time() - encode_start
    print(f"Encoding took {encode_elapsed:.0f}s", flush=True)

    # Write
    write_start = time.time()
    write_ptiles(OUTPUT_FILE, raw_blocks, index_entries, total_features)
    write_elapsed = time.time() - write_start
    print(f"Compression/write took {write_elapsed:.0f}s", flush=True)

    total_elapsed = time.time() - start
    print(f"\nTotal time: {total_elapsed:.0f}s ({total_elapsed/60:.1f}m)", flush=True)


if __name__ == "__main__":
    main()
