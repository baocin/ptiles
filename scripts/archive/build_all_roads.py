#!/usr/bin/env python3
"""
Build US.roads.ptiles from all state PBF files.

Usage:
    python build_all_roads.py /Volumes/core/timeline-ptiles-cache/raw/ /Volumes/core/timeline-ptiles-cache/tiles/US.roads.ptiles

Two-pass approach to stay under ~4 GB RAM:
  Pass 1: Process each state → encode blocks per H3 cell → write per-state pickle
           (encoded block bytes per cell, NOT raw segment dicts)
  Pass 2: Merge all state pickles → concatenate encoded records per cell →
           train dictionary, compress, write final file
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
    encode_varint, zigzag_encode, encode_coordinates, encode_string_u16,
    encode_string_u8, encode_indexed_or_custom,
    write_header, write_index, train_dictionary, compress_block,
    HEADER_SIZE,
)
from build_roads import (
    extract_roads, split_at_h3_boundaries, encode_road_record,
    ROAD_CLASS_INDEX, SURFACE_INDEX,
)

import h3


def encode_cell_records(segments: list[dict]) -> tuple[bytes, int]:
    """Encode segments for one cell as length-prefixed records sorted by OSM ID.
    Returns (encoded_bytes, segment_count)."""
    buf = bytearray()
    segments.sort(key=lambda s: s["osm_id"])
    prev_osm_id = 0
    for seg in segments:
        record = encode_road_record(seg, prev_osm_id)
        buf.extend(struct.pack("<I", len(record)))
        buf.extend(record)
        prev_osm_id = seg["osm_id"]
    return bytes(buf), len(segments)


def process_state_to_cache(pbf_path: str, cache_path: str) -> tuple[int, int]:
    """Process one state: extract → split → encode per cell → save to pickle.

    The pickle stores {cell_id: encoded_block_bytes} — much smaller than raw segments.
    Returns (road_count, segment_count).
    """
    roads = extract_roads(pbf_path)
    road_count = len(roads)

    print(f"  Splitting at H3 boundaries...")
    cell_segments = defaultdict(list)
    segment_count = 0
    for road in roads:
        segments = split_at_h3_boundaries(road)
        for seg in segments:
            cell_segments[seg["h3_cell"]].append(seg)
            segment_count += 1
    del roads

    # Encode blocks per cell — this collapses Python dicts to compact bytes
    print(f"  Encoding {len(cell_segments):,} cell blocks...")
    cell_blocks = {}
    cell_counts = {}
    for cell_id, segments in cell_segments.items():
        cell_blocks[cell_id], cell_counts[cell_id] = encode_cell_records(segments)
    del cell_segments

    # Save encoded blocks to cache
    with open(cache_path, "wb") as f:
        pickle.dump({"blocks": cell_blocks, "counts": cell_counts}, f, protocol=5)

    return road_count, segment_count


def merge_encoded_blocks(block_a: bytes, block_b: bytes) -> bytes:
    """Merge two encoded blocks by re-sorting records by OSM ID.

    Each block is a sequence of (record_length: u32, record_data: bytes).
    The first varint of each record is the delta OSM ID (from prev_osm_id=0
    or from the previous record). We need to parse IDs, merge-sort, re-encode deltas.
    """
    from shared import decode_varint

    def parse_records(data):
        """Parse encoded block into list of (osm_id, raw_record_bytes)."""
        records = []
        pos = 0
        prev_id = 0
        while pos < len(data):
            rlen = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            record = data[pos:pos + rlen]
            # First field is delta OSM ID as varint
            delta, _ = decode_varint(record, 0)
            osm_id = prev_id + delta
            records.append((osm_id, record))
            prev_id = osm_id
            pos += rlen
        return records

    recs_a = parse_records(block_a)
    recs_b = parse_records(block_b)

    # Merge and sort by OSM ID
    all_recs = sorted(recs_a + recs_b, key=lambda r: r[0])

    # Re-encode with correct deltas
    buf = bytearray()
    prev_id = 0
    for osm_id, old_record in all_recs:
        # Replace the delta varint at the start of the record
        old_delta, old_delta_len = decode_varint(old_record, 0)
        new_delta = encode_varint(osm_id - prev_id)
        new_record = new_delta + old_record[old_delta_len:]
        buf.extend(struct.pack("<I", len(new_record)))
        buf.extend(new_record)
        prev_id = osm_id

    return bytes(buf)


def build_all_roads(raw_dir: str, output_path: str):
    """Process all state PBFs and write combined US.roads.ptiles."""
    t0 = time.time()

    pbf_files = sorted(glob.glob(os.path.join(raw_dir, "*.osm.pbf")))
    if not pbf_files:
        print(f"No .osm.pbf files found in {raw_dir}")
        sys.exit(1)

    print(f"Found {len(pbf_files)} PBF files")

    cache_dir = os.path.join(os.path.dirname(output_path), "road_state_cache")
    os.makedirs(cache_dir, exist_ok=True)

    # Pass 1: Process each state into cached encoded blocks
    total_roads = 0
    total_segments = 0
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
            state_segments = sum(data["counts"].values())
            state_roads = state_segments  # approximate
            print(f"  ~{state_segments:,} segments in {len(data['blocks']):,} cells (cached)")
            del data
        else:
            state_roads, state_segments = process_state_to_cache(pbf_path, cache_path)
            print(f"  {state_roads:,} roads → {state_segments:,} segments")

        total_roads += state_roads
        total_segments += state_segments

    print(f"\n{'='*60}")
    print(f"Total: {total_roads:,} roads → {total_segments:,} segments")

    # Pass 2: Merge all state caches into final cell blocks
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
                # Border cell — merge with existing
                merged_blocks[cell_id] = merge_encoded_blocks(merged_blocks[cell_id], block_bytes)
                merged_counts[cell_id] = merged_counts.get(cell_id, 0) + data["counts"][cell_id]
            else:
                merged_blocks[cell_id] = block_bytes
                merged_counts[cell_id] = data["counts"][cell_id]

        del data

    print(f"  Merged into {len(merged_blocks):,} H3 cells")

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

    with open(output_path, "wb") as f:
        write_header(
            f,
            magic=b"PTILESR",
            version=1,
            min_lat=min_lat, min_lon=min_lon,
            max_lat=max_lat, max_lon=max_lon,
            feature_count=total_segments,
            block_count=len(sorted_cells),
            dict_offset=dict_offset, dict_length=dict_length,
            index_offset=index_offset, index_length=index_length,
            blocks_offset=blocks_offset,
        )
        f.write(dict_data)
        write_index(f, index_entries)
        for cell_id in sorted_cells:
            f.write(compressed_blocks[cell_id])

    elapsed = time.time() - t0
    file_size = os.path.getsize(output_path)
    print(f"\nDone in {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"  Output: {output_path}")
    print(f"  File size: {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MB)")
    print(f"  Segments: {total_segments:,}")
    print(f"  H3 cells: {len(sorted_cells):,}")
    print(f"  Bytes/segment: {file_size / total_segments:.1f}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <raw_dir> <output.roads.ptiles>")
        sys.exit(1)

    build_all_roads(sys.argv[1], sys.argv[2])
