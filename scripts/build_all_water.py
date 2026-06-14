#!/usr/bin/env python3
"""
Build US.water.ptiles from all state PBF files.

Usage:
    python build_all_water.py /Volumes/core/timeline-ptiles-cache/raw/ \
                               /Volumes/core/timeline-ptiles-cache/tiles/US.water.ptiles

Two-pass approach (same as build_all_roads.py):
  Pass 1: Process each state → encode water records per H3 cell → write per-state pickle
  Pass 2: Merge all state pickles → train dictionary, compress, write final file
"""

import sys
import os
import struct
import glob
import time
import pickle
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from shared import (
    encode_varint, zigzag_encode, coord_to_micro,
    encode_string_u16, encode_string_u8,
    write_header, write_index, train_dictionary, compress_block,
    HEADER_SIZE, decode_varint,
)
from build_water import (
    extract_water_from_pbf, classify_water_type, encode_water_record,
    encode_feature_table, WATER_TYPES,
)

import h3
import zstandard as zstd


def assign_to_cells_encoded(features: list[dict]) -> tuple[dict, dict, list[dict]]:
    """Assign features to H3 cells and encode records per cell.

    Returns (cell_blocks, cell_counts, large_features).
    cell_blocks maps cell_int -> encoded bytes for that cell.
    """
    cell_features = defaultdict(list)
    large_features = []
    next_feature_id = 1

    for feature in features:
        coords = feature.get("coords", [])
        if not coords:
            continue

        # Large water bodies (> 1000 vertices) go to feature table
        if feature["geom_type"] == 0 and len(coords) > 1000:
            feature_id = next_feature_id
            next_feature_id += 1
            large_features.append({
                "feature_id": feature_id,
                "name": feature.get("name") or "",
                "water_type": feature.get("water_type", 0),
                "coords": coords,
            })

            # Add reference records to all intersecting H3 cells
            seen_cells = set()
            step = max(1, len(coords) // 200)
            for i in range(0, len(coords), step):
                lon, lat = coords[i]
                try:
                    cell = h3.latlng_to_cell(lat, lon, 7)
                    cell_int = cell if isinstance(cell, int) else int(cell, 16)
                    if cell_int not in seen_cells:
                        seen_cells.add(cell_int)
                        cell_features[cell_int].append({
                            "osm_id": feature["osm_id"],
                            "geom_type": 2,
                            "ref_feature_id": feature_id,
                            "water_type": feature.get("water_type", 0),
                            "name": feature.get("name"),
                        })
                except Exception:
                    continue
            continue

        # Small features: assign to all touching H3 cells
        seen_cells = set()
        for lon, lat in coords:
            try:
                cell = h3.latlng_to_cell(lat, lon, 7)
                cell_int = cell if isinstance(cell, int) else int(cell, 16)
                seen_cells.add(cell_int)
            except Exception:
                continue

        for cell_int in seen_cells:
            cell_features[cell_int].append(feature)

    # Encode records per cell
    cell_blocks = {}
    cell_counts = {}
    for cell_int, feats in cell_features.items():
        buf = bytearray()
        prev_osm_id = 0
        for feat in feats:
            record_bytes, prev_osm_id = encode_water_record(feat, prev_osm_id)
            # Length-prefix each record for merge compatibility
            buf.extend(struct.pack("<I", len(record_bytes)))
            buf.extend(record_bytes)
        cell_blocks[cell_int] = bytes(buf)
        cell_counts[cell_int] = len(feats)

    return cell_blocks, cell_counts, large_features


def process_state_to_cache(pbf_path: str, cache_path: str) -> tuple[int, int, list[dict]]:
    """Process one state PBF → encoded cell blocks → pickle cache.
    Returns (feature_count, cell_count, large_features).
    """
    features = extract_water_from_pbf(pbf_path)
    feature_count = len(features)

    cell_blocks, cell_counts, large_features = assign_to_cells_encoded(features)
    del features

    with open(cache_path, "wb") as f:
        pickle.dump({
            "blocks": cell_blocks,
            "counts": cell_counts,
            "large": large_features,
        }, f, protocol=5)

    return feature_count, len(cell_blocks), large_features


def merge_encoded_blocks(block_a: bytes, block_b: bytes) -> bytes:
    """Merge two length-prefixed encoded blocks. Simple concatenation
    since water records use delta OSM IDs that get re-encoded at write time."""
    return block_a + block_b


def strip_length_prefixes(data: bytes) -> bytes:
    """Remove u32 length prefixes from records, return raw record bytes concatenated."""
    buf = bytearray()
    pos = 0
    while pos + 4 <= len(data):
        rlen = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if pos + rlen > len(data):
            break
        buf.extend(data[pos:pos + rlen])
        pos += rlen
    return bytes(buf)


def build_all_water(raw_dir: str, output_path: str):
    """Process all state PBFs and write combined US.water.ptiles."""
    t0 = time.time()

    pbf_files = sorted(glob.glob(os.path.join(raw_dir, "*.osm.pbf")))
    if not pbf_files:
        print(f"No .osm.pbf files found in {raw_dir}")
        sys.exit(1)

    print(f"Found {len(pbf_files)} PBF files")

    cache_dir = os.path.join(os.path.dirname(output_path), "water_state_cache")
    os.makedirs(cache_dir, exist_ok=True)

    # Pass 1: Process each state
    total_features = 0
    all_large_features = []
    state_caches = []

    for i, pbf_path in enumerate(pbf_files):
        state_name = os.path.basename(pbf_path).replace(".osm.pbf", "")
        cache_path = os.path.join(cache_dir, f"{state_name}.pkl")
        state_caches.append(cache_path)

        print(f"\n[{i+1}/{len(pbf_files)}] {state_name}")

        if os.path.exists(cache_path):
            print(f"  Loading from cache...")
            with open(cache_path, "rb") as f:
                data = pickle.load(f)
            state_features = sum(data["counts"].values())
            state_large = data.get("large", [])
            print(f"  {state_features:,} features in {len(data['blocks']):,} cells, {len(state_large)} large bodies (cached)")
            all_large_features.extend(state_large)
            del data
        else:
            feat_count, cell_count, state_large = process_state_to_cache(pbf_path, cache_path)
            print(f"  {feat_count:,} features → {cell_count:,} cells, {len(state_large)} large bodies")
            all_large_features.extend(state_large)
            total_features += feat_count

    print(f"\n{'='*60}")
    print(f"Total large water bodies across all states: {len(all_large_features)}")

    # Re-assign feature IDs to be globally unique
    for i, body in enumerate(all_large_features):
        body["feature_id"] = i + 1

    # Pass 2: Merge all state caches
    print("\nMerging state caches...")
    merged_blocks = {}
    merged_counts = {}

    for i, cache_path in enumerate(state_caches):
        state_name = os.path.basename(cache_path).replace(".pkl", "")
        print(f"  [{i+1}/{len(state_caches)}] Merging {state_name}...")

        with open(cache_path, "rb") as f:
            data = pickle.load(f)

        for cell_id, block_bytes in data["blocks"].items():
            if cell_id in merged_blocks:
                merged_blocks[cell_id] = merge_encoded_blocks(merged_blocks[cell_id], block_bytes)
                merged_counts[cell_id] = merged_counts.get(cell_id, 0) + data["counts"][cell_id]
            else:
                merged_blocks[cell_id] = block_bytes
                merged_counts[cell_id] = data["counts"][cell_id]

        del data

    total_features = sum(merged_counts.values())
    print(f"  Merged into {len(merged_blocks):,} H3 cells, {total_features:,} features")

    # Strip length prefixes for final blocks (compression input)
    print("Stripping length prefixes...")
    for cell_id in list(merged_blocks.keys()):
        merged_blocks[cell_id] = strip_length_prefixes(merged_blocks[cell_id])

    # Train dictionary
    print("Training zstd dictionary...")
    sample_keys = list(merged_blocks.keys())[:10_000]
    samples = [merged_blocks[k] for k in sample_keys if len(merged_blocks[k]) > 0]
    dict_data = train_dictionary(samples[:10_000])
    print(f"  Dictionary size: {len(dict_data):,} bytes")
    del samples

    # Compress blocks
    print("Compressing blocks...")
    compressed_blocks = {}
    done = 0
    for cell_id, raw in merged_blocks.items():
        compressed_blocks[cell_id] = compress_block(raw, dict_data)
        done += 1
        if done % 50_000 == 0:
            print(f"  {done:,}/{len(merged_blocks):,} blocks...")

    del merged_blocks

    # Encode feature table
    feature_table_raw = encode_feature_table(all_large_features) if all_large_features else b""
    if feature_table_raw:
        feature_table_compressed = zstd.ZstdCompressor(level=19).compress(feature_table_raw)
    else:
        feature_table_compressed = b""
    print(f"  Feature table: {len(all_large_features)} bodies, {len(feature_table_compressed):,} bytes compressed")

    # Write file
    print("Writing output file...")
    sorted_cells = sorted(compressed_blocks.keys())

    dict_offset = HEADER_SIZE
    dict_length = len(dict_data)
    index_offset = dict_offset + dict_length
    index_length = 4 + 19 * len(sorted_cells)
    blocks_offset = index_offset + index_length

    index_entries = []
    current_offset = blocks_offset
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")

    for cell_id in sorted_cells:
        compressed = compressed_blocks[cell_id]
        cell_int = int(cell_id, 16) if isinstance(cell_id, str) else cell_id

        try:
            lat, lng = h3.cell_to_latlng(cell_id)
        except TypeError:
            lat, lng = h3.cell_to_latlng(hex(cell_id) if isinstance(cell_id, int) else cell_id)
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
        min_lon = min(min_lon, lng)
        max_lon = max(max_lon, lng)

        index_entries.append({
            "h3_cell": cell_int,
            "block_offset": current_offset,
            "block_length": len(compressed),
            "feature_count": merged_counts.get(cell_id, 0),
        })
        current_offset += len(compressed)

    aux_offset = current_offset
    aux_length = len(feature_table_compressed)

    with open(output_path, "wb") as f:
        write_header(
            f,
            magic=b"PTILESW",
            version=1,
            min_lat=min_lat, min_lon=min_lon,
            max_lat=max_lat, max_lon=max_lon,
            feature_count=total_features,
            block_count=len(sorted_cells),
            dict_offset=dict_offset, dict_length=dict_length,
            index_offset=index_offset, index_length=index_length,
            blocks_offset=blocks_offset,
            aux_offset=aux_offset, aux_length=aux_length,
        )
        f.write(dict_data)
        write_index(f, index_entries)
        for cell_id in sorted_cells:
            f.write(compressed_blocks[cell_id])
        if feature_table_compressed:
            f.write(feature_table_compressed)

    elapsed = time.time() - t0
    file_size = os.path.getsize(output_path)
    print(f"\nDone in {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"  Output: {output_path}")
    print(f"  File size: {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MB)")
    print(f"  Features: {total_features:,}")
    print(f"  H3 cells: {len(sorted_cells):,}")
    print(f"  Large water bodies: {len(all_large_features)}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <raw_dir> <output.water.ptiles>")
        sys.exit(1)

    build_all_water(sys.argv[1], sys.argv[2])
