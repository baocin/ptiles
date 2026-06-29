#!/usr/bin/env python3
"""
Repair building index for broken .buildings_v8.ptiles files.
Uses OSM PBF data to build OSM_ID -> H3 cell mapping.
"""

import struct
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
import h3
import zstandard as zstd
import osmium

from encoding import decode_string_table, decode_varint, zigzag_decode
from shared import write_header, write_index, HEADER_SIZE

H3_RES = 7
MAGIC = b"PTILESF\x00"
VERSION = 8
HEADER_STRUCT = struct.Struct("<7sB B 3x f f f f Q I Q I Q I Q Q I 172x")
INDEX_ENTRY_SIZE = 19
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# State -> PBF file name mapping (same as build_state_v8.py)
STATE_PBF_NAMES = {
    "AL": "alabama",
    "AK": "alaska",
    "AZ": "arizona",
    "AR": "arkansas",
    "CA": "california",
    "CO": "colorado",
    "CT": "connecticut",
    "DE": "delaware",
    "DC": "district-of-columbia",
    "FL": "florida",
    "GA": "georgia",
    "HI": "hawaii",
    "ID": "idaho",
    "IL": "illinois",
    "IN": "indiana",
    "IA": "iowa",
    "KS": "kansas",
    "KY": "kentucky",
    "LA": "louisiana",
    "ME": "maine",
    "MD": "maryland",
    "MA": "massachusetts",
    "MI": "michigan",
    "MN": "minnesota",
    "MS": "mississippi",
    "MO": "missouri",
    "MT": "montana",
    "NE": "nebraska",
    "NV": "nevada",
    "NH": "new-hampshire",
    "NJ": "new-jersey",
    "NM": "new-mexico",
    "NY": "new-york",
    "NC": "north-carolina",
    "ND": "north-dakota",
    "OH": "ohio",
    "OK": "oklahoma",
    "OR": "oregon",
    "PA": "pennsylvania",
    "RI": "rhode-island",
    "SC": "south-carolina",
    "SD": "south-dakota",
    "TN": "tennessee",
    "TX": "texas",
    "UT": "utah",
    "VT": "vermont",
    "VA": "virginia",
    "WA": "washington",
    "WV": "west-virginia",
    "WI": "wisconsin",
    "WY": "wyoming",
}


def find_zstd_frames(data):
    positions = []
    pos = 0
    while True:
        pos = data.find(ZSTD_MAGIC, pos)
        if pos == -1:
            break
        positions.append(pos)
        pos += 1
    return positions


def parse_osm_id_from_block(data, n=0):
    """Parse OSM ID of the nth building from a compressed block."""
    _, pos = decode_string_table(data, 0)
    for _ in range(n):
        if pos >= len(data):
            return None
        rec_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4 + rec_len
    if pos >= len(data):
        return None
    rec_len = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    raw, consumed = decode_varint(data, pos)
    return zigzag_decode(raw)


def count_features(data):
    n = data[0]
    pos = 1
    for _ in range(n):
        slen = data[pos]
        pos += 1 + slen
    cnt = 0
    while pos < len(data):
        rec_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4 + rec_len
        cnt += 1
    return cnt


class BuildingCentroidHandler(osmium.SimpleHandler):
    """Extract buildings from PBF, compute their centroids and H3 cells."""

    def __init__(self, state_bbox):
        super().__init__()
        self.min_lon, self.min_lat, self.max_lon, self.max_lat = state_bbox
        self.osm_to_cell = {}  # osm_id -> h3 cell

    def way(self, w):
        if not any(tag.k == "building" for tag in w.tags if tag.v):
            return
        if not w.nodes:
            return
        try:
            lon_sum, lat_sum = 0.0, 0.0
            count = 0
            for node in w.nodes:
                try:
                    lat = node.location.lat
                    lon = node.location.lon
                except Exception:
                    continue
                if count == 0:
                    if not (
                        self.min_lon <= lon <= self.max_lon
                        and self.min_lat <= lat <= self.max_lat
                    ):
                        return
                lon_sum += lon
                lat_sum += lat
                count += 1
            if count < 3:
                return
            centroid_lat = lat_sum / count
            centroid_lon = lon_sum / count
            cell = h3.latlng_to_cell(centroid_lat, centroid_lon, H3_RES)
            if isinstance(cell, str):
                cell_int = int(cell, 16)
            else:
                cell_int = int(cell)
            self.osm_to_cell[w.id] = cell_int
        except Exception:
            pass


def load_state_bbox(state_abbr):
    """Get bbox for a state from the states module."""
    from states import state_bbox, get_state

    s = get_state(state_abbr)
    return state_bbox(s) if s else None


def repair_file(state_abbr, dry_run=False):
    """Repair a single state's building file using PBF data."""
    t0 = time.time()

    nfs = "/mnt/core/kino/ptiles/data/states"
    pbf_dir = "/mnt/core/timeline-ptiles-cache/raw"
    pbf_name = STATE_PBF_NAMES.get(state_abbr)
    if not pbf_name:
        print(f"  No PBF mapping for {state_abbr}", flush=True)
        return {"error": "no_pbf_mapping"}

    ptiles_path = os.path.join(nfs, f"{state_abbr}.buildings_v8.ptiles")
    pbf_path = os.path.join(pbf_dir, f"{pbf_name}.osm.pbf")

    if not os.path.exists(ptiles_path):
        print(f"  PTILES not found: {ptiles_path}", flush=True)
        return {"error": "no_ptiles"}
    if not os.path.exists(pbf_path):
        print(f"  PBF not found: {pbf_path}", flush=True)
        return {"error": "no_pbf"}

    # Read header
    with open(ptiles_path, "rb") as f:
        hdr_data = f.read(256)
    vals = HEADER_STRUCT.unpack(hdr_data)

    if vals[12] > 4:  # index_length > 4 means valid index
        print("  Valid index, skipping", flush=True)
        return {"abbr": state_abbr, "skipped": True}

    block_count = vals[8]
    dict_offset, dict_length = vals[9], vals[10]
    blocks_offset = vals[13]
    file_size = os.path.getsize(ptiles_path)

    print(f"  {block_count} blocks, {file_size:,}B", flush=True)

    # Read dict and blocks
    with open(ptiles_path, "rb") as f:
        f.seek(dict_offset)
        dict_data = f.read(dict_length)
        f.seek(blocks_offset)
        block_data = f.read(file_size - blocks_offset)

    # Get frames
    frame_starts = find_zstd_frames(block_data)
    actual = len(frame_starts)
    if actual == 0:
        return {"error": "no_blocks"}

    frame_sizes = []
    for i in range(actual):
        sz = (
            frame_starts[i + 1] - frame_starts[i]
            if i + 1 < actual
            else len(block_data) - frame_starts[i]
        )
        frame_sizes.append(sz)

    # Step 1: Parse all blocks to get OSM IDs of first building in each
    d = zstd.ZstdCompressionDict(dict_data)
    dctx = zstd.ZstdDecompressor(dict_data=d)

    first_osm_ids = set()
    all_counts = []
    total_features = 0
    for i in range(actual):
        frame = block_data[frame_starts[i] : frame_starts[i] + frame_sizes[i]]
        try:
            dec = dctx.decompress(frame)
        except:
            all_counts.append(0)
            continue
        cnt = count_features(dec)
        total_features += cnt
        all_counts.append(cnt)
        osm_id = parse_osm_id_from_block(dec)
        if osm_id is not None:
            first_osm_ids.add(osm_id)

    print(
        f"  Parsed {len(first_osm_ids)} unique first OSM IDs from {actual} blocks",
        flush=True,
    )

    # Step 2: Read PBF to get OSM ID -> H3 cell mapping
    from states import state_bbox, get_state

    bbox = state_bbox(get_state(state_abbr))

    print(f"  Reading PBF: {pbf_name}...", flush=True)
    handler = BuildingCentroidHandler(bbox)
    handler.apply_file(str(pbf_path), locations=True)

    print(f"  Got {len(handler.osm_to_cell)} buildings from PBF", flush=True)

    # Step 3: Map each block's first OSM ID to its cell
    cells = []
    failures = 0
    for i in range(actual):
        frame = block_data[frame_starts[i] : frame_starts[i] + frame_sizes[i]]
        try:
            dec = dctx.decompress(frame)
        except:
            cells.append(0)
            failures += 1
            continue
        osm_id = parse_osm_id_from_block(dec)
        if osm_id is not None and osm_id in handler.osm_to_cell:
            cells.append(handler.osm_to_cell[osm_id])
        else:
            # Try sampling up to 3 buildings
            for n in range(1, 4):
                osm_id2 = parse_osm_id_from_block(dec, n)
                if osm_id2 is not None and osm_id2 in handler.osm_to_cell:
                    cells.append(handler.osm_to_cell[osm_id2])
                    break
            else:
                failures += 1
                cells.append(0)

    print(f"  {failures} unresolved ({100 * failures // max(actual, 1)}%)", flush=True)

    # Fill zeros
    if failures:
        for i in range(actual):
            if cells[i] == 0:
                for j in range(i - 1, -1, -1):
                    if cells[j]:
                        cells[i] = cells[j]
                        break
                if cells[i] == 0:
                    for j in range(i + 1, actual):
                        if cells[j]:
                            cells[i] = cells[j]
                            break

    if dry_run:
        return {"cells": actual, "features": total_features, "failures": failures}

    # Build index
    entries = []
    cur_off = 0
    for i in range(actual):
        entries.append(
            {
                "h3_cell": cells[i],
                "block_offset": cur_off,
                "block_length": frame_sizes[i],
                "feature_count": all_counts[i],
            }
        )
        cur_off += frame_sizes[i]

    # Bbox
    all_lats, all_lons = [], []
    for cell in sorted(set(cells)):
        if cell:
            ch = hex(cell)[2:]
            lat, lon = h3.cell_to_latlng(ch)
            all_lats.append(lat)
            all_lons.append(lon)

    if not all_lats:
        return {"error": "no_valid_cells"}

    nd = HEADER_SIZE
    nl = len(dict_data)
    ni = 4 + len(entries) * INDEX_ENTRY_SIZE
    no = nd + nl + ni

    tmp = ptiles_path + ".repair"
    with open(tmp, "wb") as f:
        write_header(
            f,
            MAGIC,
            VERSION,
            min(all_lats),
            min(all_lons),
            max(all_lats),
            max(all_lons),
            total_features,
            len(entries),
            nd,
            nl,
            nd + nl,
            ni,
            no,
        )
        f.seek(nd)
        f.write(dict_data)
        f.seek(nd + nl)
        write_index(f, entries)
        f.seek(no)
        f.write(block_data)

    new_size = os.path.getsize(tmp)
    os.replace(tmp, ptiles_path)
    dt = time.time() - t0
    print(f"  Done: {new_size:,}B, {dt:.1f}s, {failures} fails", flush=True)
    return {
        "abbr": state_abbr,
        "blocks": actual,
        "features": total_features,
        "bytes": new_size,
        "time_s": round(dt, 1),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("states", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    targets = []
    if args.all:
        targets = sorted(STATE_PBF_NAMES.keys())
    elif args.states:
        targets = [s.upper() for s in args.states]

    results = []
    for s in targets:
        print(f"\n=== {s} ===", flush=True)
        try:
            r = repair_file(s, dry_run=args.dry_run)
            results.append(r)
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
            import traceback

            traceback.print_exc()
            results.append({"abbr": s, "error": str(e)})

    print("\n=== SUMMARY ===")
    for r in results:
        a = r.get("abbr", "??")
        if r.get("error"):
            print(f"  {a:2s} ERROR: {r['error']}")
        elif r.get("skipped"):
            print(f"  {a:2s} skipped")
        else:
            print(
                f"  {a:2s} {r.get('blocks', 0):5d} blk  {r.get('features', 0):8,d} feat  {r.get('time_s', 0):5.1f}s"
            )
