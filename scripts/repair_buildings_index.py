#!/usr/bin/env python3
"""
Repair building index for broken .buildings_v8.ptiles files.

The original build_state_v8.py had a bug: index_entries was empty when
index_length was computed, producing empty indices for 48/51 states.
This script reads existing compressed blocks, cross-references OSM IDs
against PBF data from /mnt/core/timeline-ptiles-cache/raw/ to reconstruct
correct H3 cell assignments, then rewrites header + index in place.

Usage:
    uv run --with osmium --with h3 --with zstandard python scripts/repair_buildings_index.py AL
    uv run --with osmium --with h3 --with zstandard python scripts/repair_buildings_index.py CA NY TX
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


class BuildingCentroidHandler(osmium.SimpleHandler):
    """Extract buildings from PBF, compute centroids and H3 cells."""

    def __init__(self, state_bbox):
        super().__init__()
        self.min_lon, self.min_lat, self.max_lon, self.max_lat = state_bbox
        self.osm_to_cell = {}  # osm_id -> h3 cell int

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
                    lat, lon = node.location.lat, node.location.lon
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
            cell = h3.latlng_to_cell(lat_sum / count, lon_sum / count, H3_RES)
            self.osm_to_cell[w.id] = (
                int(cell, 16) if isinstance(cell, str) else int(cell)
            )
        except Exception:
            pass


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


def parse_osm_id(data, n=0):
    """Parse OSM ID of the nth building (delta-encoded from block start)."""
    _, pos = decode_string_table(data, 0)
    for _ in range(n):
        if pos >= len(data):
            return None
        rl = struct.unpack_from("<I", data, pos)[0]
        pos += 4 + rl
    if pos >= len(data):
        return None
    rl = struct.unpack_from("<I", data, pos)[0]
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
        rl = struct.unpack_from("<I", data, pos)[0]
        pos += 4 + rl
        cnt += 1
    return cnt


def repair_file(
    state_abbr,
    dry_run=False,
    nfs="/mnt/core/kino/ptiles/data/states",
    pbf_dir="/mnt/core/timeline-ptiles-cache/raw",
):
    t0 = time.time()
    pbf_name = STATE_PBF_NAMES.get(state_abbr)
    if not pbf_name:
        return {"error": f"no pbf mapping for {state_abbr}"}

    ptiles_path = os.path.join(nfs, f"{state_abbr}.buildings_v8.ptiles")
    pbf_path = os.path.join(pbf_dir, f"{pbf_name}.osm.pbf")

    with open(ptiles_path, "rb") as f:
        hdr_data = f.read(256)
    vals = HEADER_STRUCT.unpack(hdr_data)

    if vals[12] > 4:
        print("  Valid index, skipping", flush=True)
        return {"abbr": state_abbr, "skipped": True}

    blk_off, dict_off, dict_len = vals[13], vals[9], vals[10]
    fs = os.path.getsize(ptiles_path)

    with open(ptiles_path, "rb") as f:
        f.seek(dict_off)
        dict_data = f.read(dict_len)
        f.seek(blk_off)
        block_data = f.read(fs - blk_off)

    frames = find_zstd_frames(block_data)
    actual = len(frames)

    frame_sizes = []
    for i in range(actual):
        sz = (
            frames[i + 1] - frames[i] if i + 1 < actual else len(block_data) - frames[i]
        )
        frame_sizes.append(sz)

    d = zstd.ZstdCompressionDict(dict_data)
    dctx = zstd.ZstdDecompressor(dict_data=d)

    # Parse OSM IDs from all blocks
    first_ids = set()
    all_counts = []
    total_feat = 0
    for i in range(actual):
        fr = block_data[frames[i] : frames[i] + frame_sizes[i]]
        try:
            dec = dctx.decompress(fr)
        except:
            all_counts.append(0)
            continue
        cnt = count_features(dec)
        total_feat += cnt
        all_counts.append(cnt)
        oid = parse_osm_id(dec)
        if oid is not None:
            first_ids.add(oid)

    # Read PBF for OSM ID -> cell mapping
    bbox = __import__("states", fromlist=["state_bbox", "get_state"]).state_bbox(
        __import__("states", fromlist=["get_state"]).get_state(state_abbr)
    )
    handler = BuildingCentroidHandler(bbox)
    handler.apply_file(str(pbf_path), locations=True)
    osm_cells = handler.osm_to_cell

    cells = []
    fails = 0
    for i in range(actual):
        fr = block_data[frames[i] : frames[i] + frame_sizes[i]]
        try:
            dec = dctx.decompress(fr)
        except:
            cells.append(0)
            fails += 1
            continue
        oid = parse_osm_id(dec)
        if oid in osm_cells:
            cells.append(osm_cells[oid])
        else:
            # Try next 3 buildings in block
            found = False
            for n in range(1, 4):
                oid2 = parse_osm_id(dec, n)
                if oid2 in osm_cells:
                    cells.append(osm_cells[oid2])
                    found = True
                    break
            if not found:
                cells.append(0)
                fails += 1

    # Fill zeros from nearest resolved cell
    if fails:
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
        return {"cells": actual, "features": total_feat, "failures": fails}

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

    all_lats, all_lons = [], []
    for cell in sorted(set(cells)):
        if cell:
            ch = hex(cell)[2:]
            lat, lon = h3.cell_to_latlng(ch)
            all_lats.append(lat)
            all_lons.append(lon)

    nd, nl = HEADER_SIZE, len(dict_data)
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
            total_feat,
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
    os.replace(tmp, ptiles_path)

    dt = time.time() - t0
    print(
        f"  Done: {os.path.getsize(ptiles_path):,}B, {dt:.0f}s, {fails} fails",
        flush=True,
    )
    return {
        "abbr": state_abbr,
        "blocks": actual,
        "features": total_feat,
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

    for s in targets:
        print(f"\n=== {s} ===", flush=True)
        try:
            r = repair_file(s, dry_run=args.dry_run)
            if r.get("error"):
                print(f"  ERROR: {r['error']}", flush=True)
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
