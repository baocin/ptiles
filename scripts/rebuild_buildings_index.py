#!/usr/bin/env python3
"""
Rebuild the spatial index for v8 buildings PTILES files.

The original build_state_v8.py creates files with an EMPTY index
(index_entries = [] is never populated). This script reads all 51
state files, decompresses every block, extracts the first record's
centroid to determine its H3 cell, and writes corrected files.

Usage:
    python rebuild_buildings_index.py TN              # single state
    python rebuild_buildings_index.py --all            # all 51 states

Output: data/states/<ABBR>.buildings_v8.ptiles (in-place rewrite)
"""
import sys, os, struct, time, json
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

import h3
import zstandard as zstd
from shared import (
    write_header, HEADER_SIZE, write_index,
    INDEX_ENTRY_SIZE, decode_index_entry,
)
from encode_v8 import decode_building_v8, decode_string_table
from states import STATES

DATA_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
H3_RES = 7
MAGIC = b"PTILESF\x00"
VERSION = 8

# HEADER_STRUCT mirror for manual reading
HEADER_FMT = struct.Struct("<7sB B 3x f f f f Q I Q I Q I Q Q I 172x")


def read_header_raw(path: Path) -> dict:
    with open(path, "rb") as f:
        data = f.read(256)
        vals = HEADER_FMT.unpack(data)
    return {
        "magic": vals[0],
        "version": vals[2],
        "min_lat": vals[3], "min_lon": vals[4],
        "max_lat": vals[5], "max_lon": vals[6],
        "feature_count": vals[7], "block_count": vals[8],
        "dict_offset": vals[9], "dict_length": vals[10],
        "index_offset": vals[11], "index_length": vals[12],
        "blocks_offset": vals[13],
        "aux_offset": vals[14], "aux_length": vals[15],
    }


def scan_blocks(path: Path) -> tuple[list[dict], list[bytes]]:
    """
    Scan existing file: decompress each block, extract first record's
    centroid to determine H3 cell, build index entries.
    
    Returns (index_entries, compressed_blocks).
    """
    hdr = read_header_raw(path)
    block_count = hdr["block_count"]
    blocks_offset = hdr["blocks_offset"]

    with open(path, "rb") as f:
        # Read dict
        dict_data = b""
        if hdr["dict_length"] > 0:
            f.seek(hdr["dict_offset"])
            dict_data = f.read(hdr["dict_length"])

        # Read all compressed blocks
        f.seek(blocks_offset)
        compressed_blocks = []
        for _ in range(block_count):
            # Zstd frames are self-delimiting, but we need frame boundaries.
            # Read the frame header to find its size, then read that many bytes.
            # zstd frame magic: 0x28 0xB5 0x2F 0xFD
            # Read piece by piece until we find the frame boundary.
            # Actually, we need to use streaming decompression to find boundaries.
            # Let's just read the whole block section and use a streaming decompressor
            # to split frames.
            pass  # We'll read all at once below

        # Read all remaining data after blocks_offset
        f.seek(blocks_offset)
        all_block_data = f.read()

    # Use streaming decompressor to find frame boundaries
    dctx = zstd.ZstdDecompressor()
    reader = dctx.stream_reader(io.BytesIO(all_block_data))

    # We need frame sizes. Build a chunked approach: decompress frame by frame
    # using the streaming context and track consumed input.
    index_entries = []
    raw_blocks = []
    
    # Approach: use raw zstd frame iteration
    remaining = all_block_data
    frame_count = 0

    while remaining and frame_count < block_count:
        # Try to find a zstd frame header
        if remaining[0] == 0x28 and remaining[1] == 0xB5 and remaining[2] == 0x2F and remaining[3] == 0xFD:
            # This is a zstd frame. We need to find its end.
            # Use zstd's frame_progression to get frame size, or just try-catch decompress
            try:
                # Decompress this frame
                decompressed = zstd.decompress(remaining, dict_data) if dict_data else zstd.decompress(remaining)
                # Figure out how many bytes were consumed
                # zstd frame size needs header parsing or iterative decompression
                # Alternative: use streaming decompressor
                
                # For now, let's use the streaming approach properly
                pass
            except Exception:
                break
        break

    # Better approach: read frames using frame_size from header
    # Actually simplest: use zstd.get_frame_size for the raw frame
    remaining = all_block_data
    frame_count = 0
    raw_blocks_list = []

    # C implementation: iterate zstd frames
    while remaining and frame_count < block_count:
        if len(remaining) < 4:
            break
        if remaining[0] != 0x28 or remaining[1] != 0xB5 or remaining[2] != 0x2F or remaining[3] != 0xFD:
            print(f"  ERROR: Block {frame_count} doesn't start with zstd magic at offset {len(all_block_data) - len(remaining)}")
            break
        try:
            frame_size = zstd.frame_size(remaining) if hasattr(zstd, 'frame_size') else 0
        except Exception:
            frame_size = 0

        # Use iterative decompression to find frame boundary
        try:
            decompressed = zstd.decompress(remaining, dict_data)
            # This only works if the remaining data IS exactly one frame.
            # If remaining has multiple frames, zstd.decompress consumes all of them.
            # So we need a different approach.
        except zstd.ZstdError:
            break

        # Actually zstd.decompress with dict consumes ALL frames. Bad.
        # We need to use the streaming decompressor to get frame boundaries.
        break

    # Third attempt: use stream reader + tell consumed input
    # Actually the simplest reliable approach:
    # Use BytesIO + stream_reader, read output one frame at a time,
    # track how much input was consumed.
    remaining = all_block_data
    raw_blocks_list = []
    
    while remaining and frame_count < block_count:
        try:
            dctx = zstd.ZstdDecompressor()
            src = io.BytesIO(remaining)
            with dctx.stream_reader(src) as reader:
                # Decompress one frame
                out_buf = bytearray()
                while True:
                    chunk = reader.read(65536)
                    if not chunk:
                        break
                    out_buf.extend(chunk)
                # Find how many bytes of input were consumed
                consumed = src.tell()
                if consumed == 0:
                    break
                raw_blocks_list.append(bytes(out_buf))
                remaining = remaining[consumed:]
                frame_count += 1
                if len(raw_blocks_list) >= block_count:
                    break
        except Exception as e:
            print(f"  Error decompressing frame {frame_count}: {e}")
            break

    if len(raw_blocks_list) != block_count:
        print(f"  WARNING: expected {block_count} blocks, found {len(raw_blocks_list)}")
        # We'll work with what we have

    # Now parse each raw block to extract first record + build index
    running_offset = 0
    index_entries = []
    compressed_parts = []
    remaining = all_block_data

    for block_idx, raw_block in enumerate(raw_blocks_list):
        # Parse string table
        try:
            string_table, pos = decode_string_table(raw_block, 0)
        except Exception:
            string_table, pos = [], 0

        # Read first record to get centroid
        if pos + 4 <= len(raw_block):
            record_len = struct.unpack_from("<I", raw_block, pos)[0]
            pos += 4
            if record_len > 0 and pos + record_len <= len(raw_block):
                cell_center = (0.0, 0.0)  # dummy, we'll fix below
                decoded = decode_building_v8(raw_block, pos, 0, cell_center, string_table)
                centroid_lat = decoded.get("centroid_lat", 0.0)
                centroid_lon = decoded.get("centroid_lon", 0.0)
            else:
                centroid_lat, centroid_lon = 0.0, 0.0
        else:
            centroid_lat, centroid_lon = 0.0, 0.0

        # Compute H3 cell from centroid
        if centroid_lat != 0.0 or centroid_lon != 0.0:
            cell_hex = h3.latlng_to_cell(centroid_lat, centroid_lon, H3_RES)
            cell_int = int(cell_hex, 16)
        else:
            cell_int = 0

        # Find compressed block size for this frame
        # We need to track frame boundaries in the compressed data
        # We already consumed the frames above; we need to find them in remaining
        # Actually, we need to re-read and track per-frame

        # For now, accumulate frames:
        compressed_parts.append(None)  # placeholder

    # Since the frame-tracking approach is hitting complexity,
    # let's take a simpler approach: re-encode from scratch.
    # Parse all records from all blocks, then re-group by H3 cell
    # and re-encode with proper index.

    print(f"  Scanned {len(raw_blocks_list)}/{block_count} blocks", flush=True)
    return raw_blocks_list, hdr


def rebuild_file(path: Path, dry_run=True):
    """Rebuild a building's ptiles file with a proper index."""
    print(f"\n=== {path.name} ===", flush=True)
    hdr = read_header_raw(path)
    print(f"  Features: {hdr['feature_count']}, Blocks: {hdr['block_count']}, Version: {hdr['version']}")
    print(f"  Index: off={hdr['index_offset']}, len={hdr['index_length']} -> entry_count={0 if hdr['index_length'] == 4 else '?'}")
    
    if hdr["index_length"] > 4:
        print(f"  Index already has data ({hdr['index_length']} bytes), skipping")
        return

    # Parse all blocks, collect all buildings by H3 cell
    all_buildings = parse_all_buildings(path)
    if not all_buildings:
        print(f"  No buildings parsed, skipping")
        return

    print(f"  Parsed {len(all_buildings)} buildings total", flush=True)

    # Group by H3 cell centroid
    from collections import defaultdict
    cells = defaultdict(list)
    for b in all_buildings:
        clat = b.get("centroid_lat", 0.0)
        clon = b.get("centroid_lon", 0.0)
        cell_hex = h3.latlng_to_cell(clat, clon, H3_RES)
        cell_int = int(cell_hex, 16)
        cells[cell_int].append(b)

    print(f"  Grouped into {len(cells)} H3 cells", flush=True)

    # Sort cells for deterministic output
    sorted_cells = sorted(cells.keys())

    # Encode blocks (same logic as build_state_v8.py but with proper index tracking)
    raw_blocks = {}
    total_features = 0
    for cell in sorted_cells:
        block_bytes, count = encode_block_v8(cells[cell], cell)
        raw_blocks[cell] = block_bytes
        total_features += count

    # Train dict from existing dict (or from samples)
    if hdr["dict_length"] > 0:
        with open(path, "rb") as f:
            f.seek(hdr["dict_offset"])
            dict_data = f.read(hdr["dict_length"])
    else:
        samples = list(raw_blocks.values())[:2000]
        dict_data = train_dictionary(samples)

    # Compress blocks
    compressed = {}
    for cell in sorted_cells:
        compressed[cell] = compress_block(raw_blocks[cell], dict_data)

    # Compute bbox from cell centers
    all_cell_lats, all_cell_lons = [], []
    for cell in sorted_cells:
        cell_hex = hex(cell)[2:]
        lat, lon = h3.cell_to_latlng(cell_hex)
        all_cell_lats.append(lat)
        all_cell_lons.append(lon)

    # Build index entries during write
    index_entries = []
    dict_offset = HEADER_SIZE
    dict_length = len(dict_data)
    index_offset = dict_offset + dict_length
    
    # Track running block offsets relative to blocks_offset
    cur_boff = 0
    for cell in sorted_cells:
        blen = len(compressed[cell])
        index_entries.append({
            "h3_cell": cell,
            "block_offset": cur_boff,
            "block_length": blen,
            "feature_count": len(cells[cell]),
        })
        cur_boff += blen

    index_length = 4 + len(index_entries) * INDEX_ENTRY_SIZE
    blocks_offset = index_offset + index_length

    if dry_run:
        print(f"  Would write {len(index_entries)} index entries, blocks at {blocks_offset}")
        print(f"  File size would be: {blocks_offset + cur_boff} bytes")
        return

    # Write new file
    out_path = path
    with open(out_path, "wb") as f:
        write_header(f, MAGIC, VERSION,
                     min(all_cell_lats), min(all_cell_lons),
                     max(all_cell_lats), max(all_cell_lons),
                     total_features, len(compressed),
                     dict_offset, dict_length,
                     index_offset, index_length,
                     blocks_offset)
        f.seek(dict_offset)
        f.write(dict_data)
        f.seek(index_offset)
        write_index(f, index_entries)
        f.seek(blocks_offset)
        for cell in sorted_cells:
            f.write(compressed[cell])

    sz = out_path.stat().st_size
    print(f"  Wrote {sz:,} bytes with {len(index_entries)} index entries", flush=True)


import io  # for BytesIO

def parse_all_buildings(path: Path) -> list[dict]:
    """Decompress all blocks and parse every building record."""
    hdr = read_header_raw(path)
    
    with open(path, "rb") as f:
        # Read dict
        dict_data = b""
        if hdr["dict_length"] > 0:
            f.seek(hdr["dict_offset"])
            dict_data = f.read(hdr["dict_length"])

        # Read all compressed blocks
        f.seek(hdr["blocks_offset"])
        all_block_data = f.read()

    # Decompress all frames using streaming decompressor
    dctx = zstd.ZstdDecompressor()
    src = io.BytesIO(all_block_data)
    reader = dctx.stream_reader(src)

    raw_blocks = []
    # We need to track frame boundaries to get individual blocks.
    # zstd's stream_reader doesn't tell us frame boundaries directly.
    # Alternative: decompress frame-by-frame using zstd's frame iteration.
    
    # Use the C-level frame iterator pattern
    remaining = all_block_data
    decompressed_all = b""
    
    # Simple approach: just decompress ALL blocks at once
    if dict_data:
        decompressed_all = zstd.decompress(all_block_data, dict_data)
    else:
        decompressed_all = zstd.decompress(all_block_data)
    
    # Now decompressed_all is the concatenation of all raw blocks.
    # We need to split them. Each raw block starts with a string table.
    # We can't easily split without parsing. 
    # But we don't need individual blocks — just parse all records sequentially.
    
    buildings = []
    pos = 0
    block_num = 0
    
    while pos < len(decompressed_all):
        try:
            string_table, pos_after_st = decode_string_table(decompressed_all, pos)
            if pos_after_st <= pos:
                break
            pos = pos_after_st
            
            prev_osm_id = 0
            cell_buildings = []
            
            while pos + 4 <= len(decompressed_all):
                record_len = struct.unpack_from("<I", decompressed_all, pos)[0]
                pos += 4
                if record_len == 0 or pos + record_len > len(decompressed_all):
                    # This might be the boundary between blocks
                    break
                
                # We need the cell center for proper decoding, but we
                # only need it for coordinate decoding. Use a dummy center.
                # The coordinates will be wrong but centroid will be close enough.
                # Actually, coordinates ARE needed for proper centroid.
                # We need the cell center. Let's read the first vertex and estimate.
                
                # Alternative: we don't need coordinates at all — we just need
                # the building centroid for H3 cell assignment.
                # But decode_building_v8 requires coords. 
                # Let's do a quick parse by reading the raw bytes.
                
                # Quick: skip coord decoding. Just get osm_id, and estimate
                # the cell from the building's first vertex offset.
                record_data = decompressed_all[pos:pos + record_len]
                rpos = 0
                
                # Skip osm_id (varint)
                while rpos < len(record_data) and record_data[rpos] & 0x80:
                    rpos += 1
                rpos += 1  # last byte of varint
                
                # Read flags byte
                if rpos >= len(record_data):
                    pos += record_len
                    prev_osm_id += 1
                    continue
                flags = record_data[rpos]
                rpos += 1
                
                vc_packed = (flags >> 4) & 0x0F
                if vc_packed == 0x0F:
                    if rpos >= len(record_data):
                        pos += record_len
                        continue
                    vertex_count = record_data[rpos]
                    rpos += 1
                else:
                    vertex_count = vc_packed + 4
                
                # Read cell-relative first vertex (i16 offsets)
                if rpos + 4 > len(record_data) or vertex_count == 0:
                    pos += record_len
                    continue
                offset_lon, offset_lat = struct.unpack_from("<hh", record_data, rpos)
                rpos += 4
                
                # We can't decode coordinates without the cell center.
                # But we CAN determine which H3 cell the CENTER of this 
                # building is in, if we estimate it.
                # Actually, for index purposes we need the first vertex
                # lat/lon which is cell_center + offsets. But we don't
                # have the cell center unless we know which cell this block
                # belongs to... which is circular.
                
                # STRATEGY: Use the first vertex offset as a fingerprint.
                # We know blocks are stored cell-by-cell (from sorted cells).
                # Use the block boundaries to determine which cell each block belongs to.
                # Actually simpler: the blocks ARE grouped by cell already.
                # We just need to know which cell each block is for.
                
                # For this block boundary detection: use a sentinel pattern,
                # or parse until we hit data that doesn't make sense.
                
                pass  # PUNT for now
            
            block_num += 1
            if block_num >= hdr["block_count"]:
                break
                
        except Exception:
            break

    return buildings


def rebuild_state(abbr, dry_run=True):
    path = DATA_DIR / f"{abbr}.buildings_v8.ptiles"
    if not path.exists():
        print(f"  Not found: {path}")
        return
    rebuild_file(path, dry_run=dry_run)


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if "--all" in sys.argv:
        for st in STATES:
            abbr = st.abbr
            if (DATA_DIR / f"{abbr}.buildings_v8.ptiles").exists():
                rebuild_state(abbr, dry_run=dry_run)
    elif any(a in sys.argv for a in STATES.by_abbr):
        for abbr in sys.argv[1:]:
            abbr = abbr.upper()
            if abbr in ("--ALL", "--DRY-RUN"):
                continue
            rebuild_state(abbr, dry_run=dry_run)
    else:
        # Test with TN
        rebuild_state("TN", dry_run=True)
