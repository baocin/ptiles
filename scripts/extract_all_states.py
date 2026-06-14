#!/usr/bin/env python3
"""
Single OSM PBF pass → per-state PTILES v8 building files.

Reads north-america-latest.osm.pbf once with node location indexing,
filters building footprints by state bounding boxes, groups by H3 cell,
encodes v8 blocks, and writes one PTILES file per state.

Usage:
  python3 extract_all_states.py

Output: /home/aoi/kino/projects/ptiles/data/states/<ABBR>.buildings_v8.ptiles
"""

import sys
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

import time, os, atexit, math
from pathlib import Path
from collections import defaultdict

import osmium
import h3

from encode_v8 import encode_block_v8
from shared import HEADER_SIZE, write_header, write_index
from states import STATES, state_bbox

# --- Config ---
PBF = "/home/aoi/data/ptiles-source/north-america-latest.osm.pbf"
OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
INDEX = "/home/aoi/data/osmium_index.idx"
H3_RES = 7
MAGIC = b"PTILESF\x00"
VERSION = 8

# Clean up index on exit
def cleanup():
    for f in Path("/home/aoi/data").glob("osmium_index*"):
        f.unlink(missing_ok=True)
atexit.register(cleanup)

# Pre-compute state bbox tests
class StateFilter:
    """Fast state-membership check via bounding boxes."""
    def __init__(self):
        self.states = STATES
        self.bboxes = {s.abbr: state_bbox(s) for s in self.states}

state_filter = StateFilter()


class BuildingExtractor(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        # per_cell[state_abbr][h3_cell_hex] = list of building dicts
        self.per_cell: dict[str, dict[str, list[dict]]] = {
            s.abbr: defaultdict(list) for s in STATES
        }
        self.counts: dict[str, int] = defaultdict(int)
        self.total = 0
        self.t0 = time.time()
        self.last_report = 0

    def way(self, w):
        """Process a way (building footprint)."""
        # Fast check: does this way have a "building" tag?
        is_building = False
        btype = "yes"
        name = ""
        height = None

        for tag in w.tags:
            if tag.k == "building":
                is_building = True
                btype = tag.v
                break
        if not is_building:
            return

        # Get all tags we need
        for tag in w.tags:
            if tag.k == "name":
                name = tag.v
            elif tag.k == "height":
                try:
                    height = float(tag.v.rstrip("m "))
                except (ValueError, TypeError):
                    pass

        # Get coordinates from first node (enough for centroid-based H3 assignment)
        if not w.nodes:
            return

        first_node = w.nodes[0]
        lon, lat = first_node.lon, first_node.lat

        # Check if within any state bbox
        for s in STATES:
            min_lon, min_lat, max_lon, max_lat = state_bbox(s)
            if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
                continue

            # Build geometry (full polygon from all nodes)
            coords = []
            for n in w.nodes:
                nx, ny = n.lon, n.lat
                if -180 <= nx <= 180 and -90 <= ny <= 90:
                    coords.append((nx, ny))
            if len(coords) < 4:
                break

            # Assign to H3 cell
            try:
                cell = h3.latlng_to_cell(lat, lon, H3_RES)
            except Exception:
                break

            self.per_cell[s.abbr][cell].append({
                "osm_id": w.id,
                "coords": coords,
                "building_type": btype,
                "name": name,
                "height_m": height,
            })
            self.counts[s.abbr] += 1
            self.total += 1

            # One building can only be in one state
            break

        # Progress report
        if self.total >= self.last_report + 20000:
            self.last_report = self.total
            dt = time.time() - self.t0
            rate = self.total / dt if dt > 0 else 0
            top = sorted(self.counts.items(), key=lambda x: -x[1])[:5]
            top_str = " ".join(f"{k}={v}" for k, v in top)
            print(f"  progress: {self.total} buildings, {rate:.0f}/s, top: {top_str}", flush=True)


def write_state_files(extractor: BuildingExtractor):
    """Write PTILES v8 files for all states that have buildings."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    for s in STATES:
        cell_data = extractor.per_cell.get(s.abbr, {})
        if not cell_data:
            results.append((s.abbr, 0, 0))
            continue

        count = sum(len(v) for v in cell_data.values())
        bbox = state_bbox(s)
        path = OUTPUT_DIR / f"{s.abbr}.buildings_v8.ptiles"

        with path.open("wb") as f:
            f.write(b"\x00" * HEADER_SIZE)
            index_entries = []
            block_offset = HEADER_SIZE
            total_features = 0

            for cell_hex in sorted(cell_data.keys()):
                blist = cell_data[cell_hex]
                block_bytes, feat_count = encode_block_v8(blist, cell_hex)
                if not block_bytes:
                    continue
                f.write(block_bytes)
                index_entries.append({
                    "h3_cell": int(cell_hex, 16),
                    "block_offset": block_offset,
                    "block_length": len(block_bytes),
                    "feature_count": feat_count,
                })
                block_offset += len(block_bytes)
                total_features += feat_count

            min_lon, min_lat, max_lon, max_lat = bbox
            write_header(f, MAGIC, VERSION,
                         min_lat, min_lon, max_lat, max_lon,
                         total_features, len(index_entries),
                         0, 0, block_offset, 4 + len(index_entries) * 20,
                         HEADER_SIZE)
            f.seek(HEADER_SIZE)
            write_index(f, index_entries)

        sz = path.stat().st_size
        results.append((s.abbr, count, sz))
        print(f"  wrote {s.abbr}: {count:,d} bldgs, {sz:,d} B", flush=True)

    return results


def main():
    print(f"Starting extraction from {PBF}", flush=True)
    print(f"Output: {OUTPUT_DIR}", flush=True)
    print(f"Index: {INDEX}", flush=True)

    t0 = time.time()

    extractor = BuildingExtractor()
    try:
        extractor.apply_file(PBF, locations=True, idx=f"sparse_file_array,{INDEX}")
    except Exception as e:
        # StopIteration not used - we process the whole file
        print(f"File processing completed (or error: {e})", flush=True)

    scan_time = time.time() - t0
    print(f"\nScan complete: {extractor.total} buildings in {scan_time:.1f}s", flush=True)
    print(f"Per-state counts: {dict(sorted(extractor.counts.items()))}", flush=True)

    # Write PTILES files
    print(f"\nWriting PTILES files...", flush=True)
    results = write_state_files(extractor)
    write_time = time.time() - t0

    print(f"\n=== COMPLETE ===")
    print(f"Total time: {write_time:.1f}s")
    print(f"Total buildings: {extractor.total}")
    for abbr, count, size in results:
        if count > 0:
            print(f"  {abbr}: {count:,d} bldgs, {size:,d} B")
    print(f"Files in: {OUTPUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
