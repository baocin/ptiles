#!/usr/bin/env python3
"""
Build a roads.ptiles file from OSM PBF data, v2 with intersection labels.

Usage:
    python build_roads.py <input.osm.pbf> <output.roads.ptiles>

Pipeline:
    1. Extract highway ways from PBF with tags + intersection nodes
    2. Split linestrings at H3 resolution 7 cell boundaries
    3. Group segments by H3 cell, sort by OSM ID
    4. Binary encode records (road segments + intersection table)
    5. Train zstd dictionary, compress blocks
    6. Write header + dictionary + index + data blocks
"""
import sys
import os
import struct
import math
import time
from collections import defaultdict

import osmium
import h3
import zstandard as zstd

sys.path.insert(0, os.path.dirname(__file__))
from shared import (
    encode_varint, zigzag_encode, encode_coordinates, encode_string_u16,
    encode_string_u8, encode_indexed_or_custom, write_header, write_index,
    train_dictionary, compress_block, coord_to_micro,
    HEADER_SIZE,
)

# --- Road Class Index ---

ROAD_CLASS_INDEX = {
    "motorway": 0, "motorway_link": 1, "trunk": 2, "trunk_link": 3,
    "primary": 4, "primary_link": 5, "secondary": 6, "tertiary": 7,
    "residential": 8, "service": 9, "track": 10, "footway": 11,
    "cycleway": 12, "path": 13, "pedestrian": 14, "tertiary_link": 15,
}

SURFACE_INDEX = {
    "paved": 0, "asphalt": 1, "concrete": 2, "unpaved": 3,
    "gravel": 4, "dirt": 5, "sand": 6, "grass": 7,
}

# Highway types to extract (skip very minor features)
HIGHWAY_TYPES = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "residential", "service", "track",
    "unclassified", "living_street",
    "footway", "cycleway", "path", "pedestrian", "bridleway", "steps",
}

# Intersection types from OSM node tags
INTERSECTION_TYPES = {
    "traffic_signals": 1,
    "stop": 2,
    "give_way": 3,
    "roundabout": 4,
}
INTERSECTION_NODE_TAGS = set(INTERSECTION_TYPES.keys())

FORMAT_VERSION = 2  # v2 adds intersection table


# --- OSM PBF Extraction ---

class RoadExtractor(osmium.SimpleHandler):
    """Extract highway ways + intersection nodes from OSM PBF."""

    def __init__(self):
        super().__init__()
        self.roads = []
        self.count = 0
        self.intersections = defaultdict(list)  # cell_id -> [(lon_micro, lat_micro, type)]

    def way(self, w):
        highway = w.tags.get("highway")
        if not highway or highway not in HIGHWAY_TYPES:
            return

        try:
            coords = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
        except osmium.InvalidLocationError:
            return

        if len(coords) < 2:
            return

        tags = {
            "highway": highway,
            "name": w.tags.get("name"),
            "ref": w.tags.get("ref"),
            "oneway": w.tags.get("oneway"),
            "maxspeed": w.tags.get("maxspeed"),
            "lanes": w.tags.get("lanes"),
            "surface": w.tags.get("surface"),
            "bridge": w.tags.get("bridge"),
            "tunnel": w.tags.get("tunnel"),
        }

        self.roads.append({
            "osm_id": w.id,
            "coords": coords,
            "tags": tags,
        })
        self.count += 1
        if self.count % 100_000 == 0:
            print(f"  Extracted {self.count:,} roads...", flush=True)

    def node(self, n):
        """Capture intersection nodes (traffic_signals, stop, give_way, roundabout)."""
        # Skip nodes without tags (most OSM nodes have none)
        if not n.tags:
            return
        for tag in n.tags:
            if tag.k == 'highway' and tag.v in INTERSECTION_NODE_TAGS:
                typ = INTERSECTION_TYPES[tag.v]
                lon_micro = round(n.lon * 100_000)
                lat_micro = round(n.lat * 100_000)
                cell = h3.latlng_to_cell(n.lat, n.lon, 7)
                self.intersections[cell].append((lon_micro, lat_micro, typ))
                return


def extract_roads(pbf_path: str) -> tuple[list[dict], dict]:
    """Extract all highway ways and intersection nodes from a PBF file.
    Returns (roads, intersections_by_cell)."""
    print(f"Extracting roads from {pbf_path}...")
    handler = RoadExtractor()
    handler.apply_file(pbf_path, locations=True)
    total_intersections = sum(len(v) for v in handler.intersections.values())
    print(f"  Total: {handler.count:,} roads, {total_intersections:,} intersection nodes")
    return handler.roads, dict(handler.intersections)


# --- H3 Cell Splitting ---

def split_at_h3_boundaries(road: dict, resolution: int = 7) -> list[dict]:
    coords = road["coords"]
    if len(coords) < 2:
        return []

    segments = []
    current_segment = [coords[0]]
    current_cell = h3.latlng_to_cell(coords[0][1], coords[0][0], resolution)

    for i in range(1, len(coords)):
        lon, lat = coords[i]
        cell = h3.latlng_to_cell(lat, lon, resolution)

        if cell != current_cell:
            current_segment.append((lon, lat))
            if len(current_segment) >= 2:
                segments.append({
                    "osm_id": road["osm_id"],
                    "coords": current_segment,
                    "h3_cell": current_cell,
                    "tags": road["tags"],
                })
            current_segment = [(lon, lat)]
            current_cell = cell
        else:
            current_segment.append((lon, lat))

    if len(current_segment) >= 2:
        segments.append({
            "osm_id": road["osm_id"],
            "coords": current_segment,
            "h3_cell": current_cell,
            "tags": road["tags"],
        })

    return segments


# --- Binary Encoding ---

def encode_road_record(segment: dict, prev_osm_id: int) -> bytes:
    buf = bytearray()
    tags = segment["tags"]
    coords = segment["coords"]

    buf.extend(encode_varint(segment["osm_id"] - prev_osm_id))
    buf.extend(struct.pack("<H", len(coords)))
    delta_bytes, first_lon, first_lat = encode_coordinates(coords)
    buf.extend(struct.pack("<ii", first_lon, first_lat))
    buf.extend(delta_bytes)

    flags = 0
    name = tags.get("name")
    ref = tags.get("ref")
    oneway_val = tags.get("oneway")
    maxspeed = tags.get("maxspeed")
    lanes = tags.get("lanes")
    surface = tags.get("surface")
    bridge = tags.get("bridge")
    tunnel = tags.get("tunnel")

    if name:
        flags |= 0x01
    if ref:
        flags |= 0x02
    if oneway_val and oneway_val in ("yes", "true", "1", "-1", "reverse"):
        flags |= 0x04
    if maxspeed:
        try:
            int(maxspeed.split()[0])
            flags |= 0x08
        except (ValueError, IndexError):
            pass
    if lanes:
        try:
            int(lanes)
            flags |= 0x10
        except ValueError:
            pass
    if surface:
        flags |= 0x20
    if bridge in ("yes", "viaduct") or tunnel in ("yes", "building_passage"):
        flags |= 0x40

    buf.append(flags)
    highway = tags.get("highway", "unclassified")
    buf.extend(encode_indexed_or_custom(highway, ROAD_CLASS_INDEX))

    if flags & 0x01:
        buf.extend(encode_string_u16(name))
    if flags & 0x02:
        buf.extend(encode_string_u8(ref))
    if flags & 0x04:
        if oneway_val in ("-1", "reverse"):
            buf.append(2)
        else:
            buf.append(1)
    if flags & 0x08:
        try:
            speed = int(maxspeed.split()[0])
            if "mph" in maxspeed:
                speed = round(speed * 1.60934)
            buf.append(min(speed, 255))
        except (ValueError, IndexError):
            buf.append(0)
    if flags & 0x10:
        try:
            buf.append(min(int(lanes), 255))
        except ValueError:
            buf.append(0)
    if flags & 0x20:
        buf.extend(encode_indexed_or_custom(surface, SURFACE_INDEX))
    if flags & 0x40:
        if tunnel in ("yes", "building_passage"):
            buf.append(2)
        else:
            buf.append(1)

    return bytes(buf)


def encode_block(segments: list[dict], intersections: list[tuple[int, int, int]] | None = None) -> bytes:
    """Encode road segments + optional intersection table for one H3 cell.

    Block format:
      [road records with u32 length prefixes]...
      u32 0  (sentinel marking end of road records)
      u16 intersection_count
      [i32 lon_micro, i32 lat_micro, u8 type] * intersection_count
    """
    buf = bytearray()
    prev_osm_id = 0

    segments.sort(key=lambda s: s["osm_id"])

    for seg in segments:
        record = encode_road_record(seg, prev_osm_id)
        buf.extend(struct.pack("<I", len(record)))
        buf.extend(record)
        prev_osm_id = seg["osm_id"]

    # Sentinel: 0-length record marks end of road section
    buf.extend(b"\x00\x00\x00\x00")

    # Intersection table
    if intersections:
        buf.extend(struct.pack("<H", len(intersections)))
        for lon_micro, lat_micro, typ in intersections:
            buf.extend(struct.pack("<ii", lon_micro, lat_micro))
            buf.append(typ)
    else:
        buf.extend(b"\x00\x00")  # count=0

    return bytes(buf)


# --- Main Build Pipeline ---

def build_roads_ptiles(pbf_path: str, output_path: str):
    t0 = time.time()

    # Step 1: Extract roads + intersections from PBF
    roads, intersections_by_cell = extract_roads(pbf_path)

    # Step 2: Split at H3 boundaries
    print("Splitting roads at H3 cell boundaries...")
    cell_segments = defaultdict(list)
    total_segments = 0

    for road in roads:
        segments = split_at_h3_boundaries(road)
        for seg in segments:
            cell_segments[seg["h3_cell"]].append(seg)
            total_segments += 1

    print(f"  {len(roads):,} roads -> {total_segments:,} segments in {len(cell_segments):,} H3 cells")
    del roads

    # Step 3: Encode blocks (road segments + intersections)
    print("Encoding blocks...")
    raw_blocks = {}
    for cell_id, segments in cell_segments.items():
        cell_ints = intersections_by_cell.get(cell_id, [])
        raw_blocks[cell_id] = encode_block(segments, cell_ints)

    # Count total intersections encoded
    total_intersections = sum(len(intersections_by_cell.get(c, [])) for c in cell_segments)
    print(f"  Intersections encoded: {total_intersections:,}")

    # Step 4: Train dictionary
    print("Training zstd dictionary...")
    sample_keys = list(raw_blocks.keys())[:10_000]
    samples = [raw_blocks[k] for k in sample_keys if len(raw_blocks[k]) > 0]
    if len(samples) < 100:
        samples = list(raw_blocks.values())[:10_000]
    dict_data = train_dictionary(samples)
    print(f"  Dictionary size: {len(dict_data):,} bytes")

    # Step 5: Compress
    print("Compressing blocks...")
    compressed_blocks = {}
    for cell_id, raw in raw_blocks.items():
        compressed_blocks[cell_id] = compress_block(raw, dict_data)
    del raw_blocks

    # Step 6: Layout and write
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
        seg_count = len(cell_segments[cell_id])
        lat, lng = h3.cell_to_latlng(cell_id)
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
        min_lon = min(min_lon, lng)
        max_lon = max(max_lon, lng)
        index_entries.append({
            "h3_cell": cell_int,
            "block_offset": current_offset,
            "block_length": len(compressed),
            "feature_count": seg_count,
        })
        current_offset += len(compressed)

    with open(output_path, "wb") as f:
        write_header(f, magic=b"PTILESR", version=FORMAT_VERSION,
                     min_lat=min_lat, min_lon=min_lon,
                     max_lat=max_lat, max_lon=max_lon,
                     feature_count=total_segments,
                     block_count=len(sorted_cells),
                     dict_offset=dict_offset, dict_length=dict_length,
                     index_offset=index_offset, index_length=index_length,
                     blocks_offset=blocks_offset)
        f.write(dict_data)
        write_index(f, index_entries)
        for cell_id in sorted_cells:
            f.write(compressed_blocks[cell_id])

    elapsed = time.time() - t0
    file_size = os.path.getsize(output_path)
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Output: {output_path}")
    print(f"  File size: {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MB)")
    print(f"  Segments: {total_segments:,}")
    print(f"  Intersections: {total_intersections:,}")
    print(f"  H3 cells: {len(sorted_cells):,}")
    print(f"  Format version: {FORMAT_VERSION}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.osm.pbf> <output.roads.ptiles>")
        sys.exit(1)
    build_roads_ptiles(sys.argv[1], sys.argv[2])
