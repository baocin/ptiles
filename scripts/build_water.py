#!/usr/bin/env python3
"""
Build water PTiles files from OpenStreetMap PBF data.

Extracts water bodies (polygons) and waterways (linestrings) from OSM,
assigns them to H3 cells, and writes per-state or all-US .water.ptiles files.

Usage:
    # Single state from Overpass API (small areas):
    python build_water.py --source overpass --region tennessee --output TN.water.ptiles

    # From pre-downloaded OSM PBF:
    python build_water.py --source pbf --pbf us-latest.osm.pbf --region tennessee --output TN.water.ptiles

    # Full US (requires us-latest.osm.pbf from Geofabrik, ~11 GB):
    python build_water.py --source pbf --pbf us-latest.osm.pbf --output US.water.ptiles

    # Per-state batch, one state extract each, into OUTPUT_DIR:
    python build_water.py --all
    python build_water.py --states TN,RI

Required packages:
    pip install osmium h3 zstandard shapely

Data sources:
    - OSM PBF: https://download.geofabrik.de/north-america/us-latest.osm.pbf
    - Overpass API: https://overpass-api.de/api/interpreter
"""

import sys
import os
import struct
import time
import json
import argparse
from collections import defaultdict
from pathlib import Path

import h3
import zstandard as zstd

sys.path.insert(0, os.path.dirname(__file__))
from shared import (
    choose_dictionary,
    write_header, HEADER_SIZE,
    encode_varint, zigzag_encode, coord_to_micro,
    encode_string_u16, encode_string_u8,
    encode_index_entry, train_dictionary,
)
from boundaries import stamp_boundary
from states import (
    STATES as _STATES,  # --all stays US-only
    REGIONS as _REGIONS,  # bbox table covers non-US regions too
    get_state,
    pbf_path as find_pbf,
)

# Per-state batch build, matching the other builders: PBF_DIR in, OUTPUT_DIR
# out, one {ST}.water_v{VERSION}.ptiles per state.
PBF_DIR = Path("/mnt/core/timeline-ptiles-cache/2026-08-06/pbf")
OUTPUT_DIR = Path("/mnt/core/kino/ptiles/data/states")
VERSION = 1

# Water type enum (matches Rust WATER_TYPE_REVERSE)
WATER_TYPES = {
    "lake": 0, "reservoir": 1, "pond": 2, "river": 3, "stream": 4,
    "creek": 5, "canal": 6, "drain": 7, "bay": 8, "ocean": 9,
    "wetland": 10, "marsh": 11, "swamp": 12, "estuary": 13,
}

# Bounding boxes for US states, derived from states.py so there is one source of
# truth. This used to be a hand-written table holding 5 of 51 states, which meant
# --region silently resolved to None -- no clipping at all -- for the other 46.
#
# NOTE the component order differs from states.py and is not interchangeable:
# states.py is (min_lon, min_lat, max_lon, max_lat); everything here is
# (south, west, north, east) == (min_lat, min_lon, max_lat, max_lon), unpacked
# that way in _in_bbox below.
STATE_BBOX = {}
for _s in _REGIONS:
    _box = (_s.min_lat, _s.min_lon, _s.max_lat, _s.max_lon)
    STATE_BBOX[_s.name.lower().replace(" ", "-")] = _box
    STATE_BBOX[_s.abbr.lower()] = _box
del _s, _box


def classify_water_type(tags: dict) -> int:
    """Classify an OSM feature into a water_type enum value."""
    natural = tags.get("natural", "")
    water = tags.get("water", "")
    waterway = tags.get("waterway", "")

    if water == "lake" or natural == "water" and not water:
        return WATER_TYPES["lake"]
    if water == "reservoir":
        return WATER_TYPES["reservoir"]
    if water == "pond":
        return WATER_TYPES["pond"]
    if water == "river" or waterway == "river" or waterway == "riverbank":
        return WATER_TYPES["river"]
    if waterway == "stream":
        return WATER_TYPES["stream"]
    if waterway == "creek":
        return WATER_TYPES["creek"]
    if waterway == "canal" or waterway == "ditch":
        return WATER_TYPES["canal"]
    if waterway == "drain":
        return WATER_TYPES["drain"]
    if water == "bay":
        return WATER_TYPES["bay"]
    if natural == "coastline":
        return WATER_TYPES["ocean"]
    if natural == "wetland" or water == "wetland":
        return WATER_TYPES["wetland"]
    if water == "marsh":
        return WATER_TYPES["marsh"]
    if water == "swamp":
        return WATER_TYPES["swamp"]
    if water == "estuary":
        return WATER_TYPES["estuary"]

    # Fallback
    if waterway:
        return WATER_TYPES["stream"]
    return WATER_TYPES["lake"]


def encode_water_record(feature: dict, prev_osm_id: int) -> tuple[bytes, int]:
    """Encode a single water feature record. Returns (bytes, new_prev_osm_id)."""
    buf = bytearray()

    osm_id = feature["osm_id"]
    delta = osm_id - prev_osm_id

    # osm_id: varint delta (zigzag encoded)
    buf.extend(encode_varint(zigzag_encode(delta)))

    # geom_type: u8 (0=polygon, 1=linestring, 2=reference)
    geom_type = feature["geom_type"]
    buf.append(geom_type)

    if geom_type == 2:
        # Reference: just u32 feature_id
        buf.extend(struct.pack("<I", feature["ref_feature_id"]))
    else:
        coords = feature["coords"]
        vertex_count = len(coords)
        buf.extend(struct.pack("<H", vertex_count))

        if vertex_count > 0:
            first_lon = coord_to_micro(coords[0][0])
            first_lat = coord_to_micro(coords[0][1])
            buf.extend(struct.pack("<ii", first_lon, first_lat))

            prev_lon, prev_lat = first_lon, first_lat
            for lon, lat in coords[1:]:
                cur_lon = coord_to_micro(lon)
                cur_lat = coord_to_micro(lat)
                buf.extend(encode_varint(zigzag_encode(cur_lon - prev_lon)))
                buf.extend(encode_varint(zigzag_encode(cur_lat - prev_lat)))
                prev_lon, prev_lat = cur_lon, cur_lat

    # flags: u8
    flags = 0
    name = feature.get("name")
    width = feature.get("width")
    if name:
        flags |= 0x01
    if width:
        flags |= 0x02
    buf.append(flags)

    # water_type: u8
    water_type = feature.get("water_type", 0)
    buf.append(water_type)

    # Optional fields
    if name:
        buf.extend(encode_string_u16(name))
    if width:
        buf.extend(struct.pack("<H", width))

    return bytes(buf), osm_id


def encode_feature_table(large_bodies: list[dict]) -> bytes:
    """Encode the feature table for large water bodies (aux section)."""
    buf = bytearray()
    buf.extend(struct.pack("<I", len(large_bodies)))

    for body in large_bodies:
        # feature_id: u32
        buf.extend(struct.pack("<I", body["feature_id"]))

        # name: u16 len + UTF-8
        name = body.get("name") or ""
        buf.extend(encode_string_u16(name))

        # water_type: u8
        buf.append(body.get("water_type", 0))

        # vertex_count: u32
        coords = body["coords"]
        buf.extend(struct.pack("<I", len(coords)))

        # coordinates: i32 first + zigzag varint deltas
        if coords:
            first_lon = coord_to_micro(coords[0][0])
            first_lat = coord_to_micro(coords[0][1])
            buf.extend(struct.pack("<ii", first_lon, first_lat))

            prev_lon, prev_lat = first_lon, first_lat
            for lon, lat in coords[1:]:
                cur_lon = coord_to_micro(lon)
                cur_lat = coord_to_micro(lat)
                buf.extend(encode_varint(zigzag_encode(cur_lon - prev_lon)))
                buf.extend(encode_varint(zigzag_encode(cur_lat - prev_lat)))
                prev_lon, prev_lat = cur_lon, cur_lat

    return bytes(buf)


def extract_water_from_overpass(bbox: tuple[float, float, float, float]) -> list[dict]:
    """Extract water features from Overpass API for a bounding box."""
    import requests

    south, west, north, east = bbox
    query = f"""
    [out:json][timeout:300];
    (
      way["natural"="water"]({south},{west},{north},{east});
      relation["natural"="water"]({south},{west},{north},{east});
      way["waterway"]({south},{west},{north},{east});
      way["natural"="coastline"]({south},{west},{north},{east});
    );
    out body;
    >;
    out skel qt;
    """

    print(f"Querying Overpass API for bbox ({south}, {west}, {north}, {east})...")
    resp = requests.post(
        "https://overpass-api.de/api/interpreter",
        data={"data": query},
        timeout=600,
    )
    resp.raise_for_status()
    data = resp.json()

    # Build node lookup
    nodes = {}
    for elem in data["elements"]:
        if elem["type"] == "node":
            nodes[elem["id"]] = (elem["lon"], elem["lat"])

    # Extract features
    features = []
    for elem in data["elements"]:
        if elem["type"] != "way":
            continue

        tags = elem.get("tags", {})
        if not (tags.get("natural") in ("water", "coastline") or tags.get("waterway") or tags.get("water")):
            continue

        coords = []
        for nd in elem.get("nodes", []):
            if nd in nodes:
                coords.append(nodes[nd])

        if len(coords) < 2:
            continue

        water_type = classify_water_type(tags)
        is_area = tags.get("natural") == "water" or tags.get("water")
        geom_type = 0 if is_area else 1  # polygon or linestring

        features.append({
            "osm_id": elem["id"],
            "geom_type": geom_type,
            "water_type": water_type,
            "coords": coords,
            "name": tags.get("name"),
            "width": None,
        })

    print(f"  Extracted {len(features)} water features")
    return features


def extract_water_from_pbf(pbf_path: str, bbox: tuple[float, float, float, float] | None = None) -> list[dict]:
    """Extract water features from an OSM PBF file."""
    import osmium

    class WaterHandler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.features = []
            self.count = 0

        def area(self, a):
            tags = dict(a.tags)
            if not (tags.get("natural") in ("water", "coastline") or tags.get("water")):
                return

            for ring in a.outer_rings():
                coords = [(n.lon, n.lat) for n in ring]
                if bbox and not self._in_bbox(coords):
                    continue

                water_type = classify_water_type(tags)
                self.features.append({
                    "osm_id": a.orig_id(),
                    "geom_type": 0,  # polygon
                    "water_type": water_type,
                    "coords": coords,
                    "name": tags.get("name"),
                    "width": None,
                })

            self.count += 1
            if self.count % 10000 == 0:
                print(f"  Processed {self.count} areas, {len(self.features)} features...")

        def way(self, w):
            tags = dict(w.tags)
            if not tags.get("waterway"):
                return

            try:
                coords = [(n.lon, n.lat) for n in w.nodes]
            except osmium.InvalidLocationError:
                return

            if len(coords) < 2:
                return
            if bbox and not self._in_bbox(coords):
                return

            water_type = classify_water_type(tags)
            self.features.append({
                "osm_id": w.id,
                "geom_type": 1,  # linestring
                "water_type": water_type,
                "coords": coords,
                "name": tags.get("name"),
                "width": None,
            })

        def _in_bbox(self, coords):
            if not bbox:
                return True
            south, west, north, east = bbox
            return any(south <= lat <= north and west <= lon <= east
                       for lon, lat in coords)

    print(f"Extracting water from {pbf_path}...")
    handler = WaterHandler()
    handler.apply_file(pbf_path, locations=True)
    print(f"  Total: {len(handler.features)} water features")
    return handler.features


def assign_to_h3_cells(features: list[dict]) -> dict[int, list[dict]]:
    """Assign features to H3 resolution 7 cells."""
    print("Assigning features to H3 cells...")
    cell_features = defaultdict(list)
    large_features = []
    next_feature_id = 1

    for feature in features:
        coords = feature["coords"]
        if not coords:
            continue

        # Large water bodies (> 1000 vertices) go to feature table
        if feature["geom_type"] == 0 and len(coords) > 1000:
            feature_id = next_feature_id
            next_feature_id += 1
            large_features.append({
                "feature_id": feature_id,
                "name": feature.get("name", ""),
                "water_type": feature.get("water_type", 0),
                "coords": coords,
            })

            # Add reference records to all H3 cells this body covers
            seen_cells = set()
            step = max(1, len(coords) // 200)  # sample every N vertices
            for i in range(0, len(coords), step):
                lon, lat = coords[i]
                try:
                    cell = h3.latlng_to_cell(lat, lon, 7)
                    cell_int = cell if isinstance(cell, int) else int(cell, 16)
                    if cell_int not in seen_cells:
                        seen_cells.add(cell_int)
                        cell_features[cell_int].append({
                            "osm_id": feature["osm_id"],
                            "geom_type": 2,  # reference
                            "ref_feature_id": feature_id,
                            "water_type": feature.get("water_type", 0),
                            "name": feature.get("name"),
                        })
                except Exception:
                    continue
            continue

        # Small features: assign to all H3 cells they touch
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

    print(f"  {len(cell_features)} H3 cells, {len(large_features)} large bodies in feature table")
    return dict(cell_features), large_features


def build_water_ptiles(features: list[dict], output_path: str, region_obj=None):
    """Build the .water.ptiles file."""
    t0 = time.time()

    # Assign to H3 cells
    cell_features, large_features = assign_to_h3_cells(features)

    # Encode blocks per cell
    print("Encoding cell blocks...")
    raw_blocks = []
    for cell_int in sorted(cell_features.keys()):
        cell_feats = cell_features[cell_int]
        buf = bytearray()
        prev_osm_id = 0
        for feat in cell_feats:
            record_bytes, prev_osm_id = encode_water_record(feat, prev_osm_id)
            buf.extend(record_bytes)
        raw_blocks.append((cell_int, bytes(buf), len(cell_feats)))

    # Train zstd dictionary on sample blocks
    print("Training zstd dictionary...")
    sample_blocks = [b[1] for b in raw_blocks[:min(500, len(raw_blocks))]]
    if sample_blocks:
        dict_data = choose_dictionary([b[1] for b in raw_blocks], level=12)
    else:
        dict_data = b""

    # Compress blocks
    print("Compressing blocks...")
    if dict_data:
        d = zstd.ZstdCompressionDict(dict_data)
        cctx = zstd.ZstdCompressor(level=12, dict_data=d)
    else:
        cctx = zstd.ZstdCompressor(level=12)

    compressed_blocks = []
    for cell_int, raw_data, feat_count in raw_blocks:
        compressed = cctx.compress(raw_data)
        compressed_blocks.append((cell_int, compressed, feat_count))

    # Encode feature table (aux section)
    feature_table_raw = encode_feature_table(large_features) if large_features else b""
    if feature_table_raw:
        feature_table_compressed = zstd.ZstdCompressor(level=19).compress(feature_table_raw)
    else:
        feature_table_compressed = b""

    # Compute layout
    dict_offset = HEADER_SIZE
    dict_length = len(dict_data)

    index_count = len(compressed_blocks)
    index_data_size = 4 + index_count * 19  # 4 byte count + 19 bytes per entry
    index_offset = dict_offset + dict_length

    blocks_offset = index_offset + index_data_size

    # Build index entries with block offsets
    current_offset = blocks_offset
    index_entries = []
    for cell_int, compressed, feat_count in compressed_blocks:
        index_entries.append({
            "h3_cell": cell_int,
            "block_offset": current_offset,
            "block_length": len(compressed),
            "feature_count": feat_count,
        })
        current_offset += len(compressed)

    aux_offset = current_offset
    aux_length = len(feature_table_compressed)

    # Compute bounding box
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")
    for feat in features[:10000]:  # sample for speed
        for lon, lat in feat.get("coords", [])[:10]:
            min_lat = min(min_lat, lat)
            max_lat = max(max_lat, lat)
            min_lon = min(min_lon, lon)
            max_lon = max(max_lon, lon)

    total_features = sum(fc for _, _, fc in compressed_blocks)

    # Write file
    print("Writing output file...")
    with open(output_path, "wb") as f:
        write_header(
            f,
            magic=b"PTILESW",
            version=VERSION,
            min_lat=min_lat, min_lon=min_lon,
            max_lat=max_lat, max_lon=max_lon,
            feature_count=total_features,
            block_count=len(compressed_blocks),
            dict_offset=dict_offset, dict_length=dict_length,
            index_offset=index_offset, index_length=index_data_size,
            blocks_offset=blocks_offset,
            aux_offset=aux_offset, aux_length=aux_length,
        )

        # Dictionary
        f.write(dict_data)

        # Index
        f.write(struct.pack("<I", len(index_entries)))
        for entry in index_entries:
            f.write(encode_index_entry(
                entry["h3_cell"],
                entry["block_offset"],
                entry["block_length"],
                entry["feature_count"],
            ))

        # Blocks
        for _, compressed, _ in compressed_blocks:
            f.write(compressed)

        # Feature table (aux)
        if feature_table_compressed:
            f.write(feature_table_compressed)

    if region_obj is not None:
        stamp_boundary(output_path, region_obj)

    elapsed = time.time() - t0
    file_size = os.path.getsize(output_path)
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Output: {output_path}")
    print(f"  File size: {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MB)")
    print(f"  Features: {total_features:,}")
    print(f"  H3 cells: {len(compressed_blocks):,}")
    print(f"  Large water bodies: {len(large_features)}")


def build_state(state) -> bool:
    """Build one state from its own PBF extract. False means nothing was written."""
    out = OUTPUT_DIR / f"{state.abbr}.water_v{VERSION}.ptiles"
    if out.exists():
        print(f"  SKIP {state.abbr} -- already exists", flush=True)
        return False
    # The Geofabrik file name is the state name lowercased with spaces hyphenated;
    pbf = find_pbf(state, prefer=PBF_DIR)
    if pbf is None:
        print(f"  ERROR {state.abbr}: no pbf extract found", flush=True)
        return False

    print(f"\n=== {state.abbr} {state.name} ===", flush=True)
    t0 = time.time()
    features = extract_water_from_pbf(str(pbf), STATE_BBOX[state.abbr.lower()])
    if not features:
        print(f"  {state.abbr}  0 features -- nothing written", flush=True)
        return False
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    build_water_ptiles(features, str(out), region_obj=state)
    print(f"  {state.abbr:2s} {len(features):8,d} features  "
          f"{out.stat().st_size:10,d} B  {time.time() - t0:6.1f}s", flush=True)
    return True


def main():
    parser = argparse.ArgumentParser(description="Build water PTiles files from OSM data")
    parser.add_argument("--source", choices=["overpass", "pbf"], default="overpass",
                        help="Data source (overpass or pbf)")
    parser.add_argument("--pbf", help="Path to OSM PBF file (required for --source pbf)")
    parser.add_argument("--region", help="Region name (e.g., tennessee)")
    parser.add_argument("--output", help="Output .water.ptiles file path (single-region mode)")
    parser.add_argument("--all", action="store_true",
                        help="Build every state from PBF_DIR into OUTPUT_DIR")
    parser.add_argument("--states",
                        help="Comma-separated abbreviations to build into OUTPUT_DIR (e.g. TN,RI)")
    args = parser.parse_args()

    if args.all or args.states:
        if args.all:
            targets = list(_STATES)
        else:
            targets = []
            for a in args.states.split(","):
                s = get_state(a.strip())
                if s:
                    targets.append(s)
                else:
                    print(f"  ERROR unknown state '{a.strip()}'")
        built = 0
        for s in targets:
            try:
                built += build_state(s)
            except Exception as e:
                print(f"  ERROR {s.abbr}: {e}", flush=True)
                import traceback
                traceback.print_exc()
        print(f"\nWATER_COMPLETE built={built} of {len(targets)}")
        return

    if not args.output:
        parser.error("--output is required unless --all or --states is given")

    if args.source == "overpass":
        if not args.region:
            print("Error: --region required for Overpass source")
            sys.exit(1)
        region = args.region.lower()
        if region not in STATE_BBOX:
            print(f"Error: Unknown region '{region}'. Available: {', '.join(STATE_BBOX.keys())}")
            sys.exit(1)
        features = extract_water_from_overpass(STATE_BBOX[region])
    else:
        if not args.pbf:
            print("Error: --pbf required for PBF source")
            sys.exit(1)
        bbox = STATE_BBOX.get(args.region.lower()) if args.region else None
        features = extract_water_from_pbf(args.pbf, bbox)

    build_water_ptiles(features, args.output)


if __name__ == "__main__":
    main()
