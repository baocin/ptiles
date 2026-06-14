#!/usr/bin/env python3
"""
Read and query a roads.ptiles file.

Usage:
    python read_roads.py <file.roads.ptiles> <lat> <lng>
    python read_roads.py TN.roads.ptiles 36.1627 -86.7816   # Nashville, near I-40

Returns the nearest road(s) to the query point.
"""

import sys
import os
import struct
import math
import json

sys.path.insert(0, os.path.dirname(__file__))
from shared import (
    decode_varint, zigzag_decode, decode_coordinates, decode_string_u16,
    decode_string_u8, decode_indexed_or_custom, read_header,
    read_index, binary_search_index, decompress_block,
    HEADER_SIZE,
)

import h3

# --- Road Class Reverse Index ---

ROAD_CLASS_REVERSE = {
    0: "motorway", 1: "motorway_link", 2: "trunk", 3: "trunk_link",
    4: "primary", 5: "primary_link", 6: "secondary", 7: "tertiary",
    8: "residential", 9: "service", 10: "track", 11: "footway",
    12: "cycleway", 13: "path", 14: "pedestrian", 15: "tertiary_link",
}

SURFACE_REVERSE = {
    0: "paved", 1: "asphalt", 2: "concrete", 3: "unpaved",
    4: "gravel", 5: "dirt", 6: "sand", 7: "grass",
}


# --- Road Record Decoder ---

def decode_road(data: bytes, offset: int, prev_osm_id: int = 0) -> tuple[dict, int]:
    """Decode a road segment record. Returns (road_dict, bytes_consumed)."""
    pos = offset

    # OSM way ID (delta varint)
    delta, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + delta

    # Vertex count (uint16)
    vertex_count = struct.unpack_from("<H", data, pos)[0]
    pos += 2

    # First coordinate
    first_lon, first_lat = struct.unpack_from("<ii", data, pos)
    pos += 8

    # Delta coordinates
    coords, consumed = decode_coordinates(data, pos, first_lon, first_lat, vertex_count)
    pos += consumed

    # Flags
    flags = data[pos]
    pos += 1

    # Road class
    road_class, consumed = decode_indexed_or_custom(data, pos, ROAD_CLASS_REVERSE)
    pos += consumed

    road = {
        "osm_id": osm_id,
        "road_class": road_class,
        "coords": coords,
    }

    # Optional fields
    if flags & 0x01:
        road["name"], consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x02:
        road["ref"], consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x04:
        ow = data[pos]
        pos += 1
        road["oneway"] = {0: "no", 1: "forward", 2: "reverse"}.get(ow, "no")
    if flags & 0x08:
        road["speed_limit_kmh"] = data[pos]
        pos += 1
    if flags & 0x10:
        road["lanes"] = data[pos]
        pos += 1
    if flags & 0x20:
        road["surface"], consumed = decode_indexed_or_custom(data, pos, SURFACE_REVERSE)
        pos += consumed
    if flags & 0x40:
        bt = data[pos]
        pos += 1
        road["bridge_tunnel"] = {0: None, 1: "bridge", 2: "tunnel"}.get(bt)

    return road, pos - offset


def decode_block(data: bytes) -> list[dict]:
    """Decode all road records from a decompressed block."""
    roads = []
    pos = 0
    prev_osm_id = 0
    while pos < len(data):
        record_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        road, _ = decode_road(data, pos, prev_osm_id)
        prev_osm_id = road["osm_id"]
        roads.append(road)
        pos += record_len
    return roads


# --- Distance Calculation ---

def point_to_segment_dist_sq(px, py, x1, y1, x2, y2):
    """Squared distance from point to line segment (in coordinate units)."""
    dx, dy = x2 - x1, y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq == 0:
        dx2, dy2 = px - x1, py - y1
        return dx2 * dx2 + dy2 * dy2
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    dx2, dy2 = px - proj_x, py - proj_y
    return dx2 * dx2 + dy2 * dy2


def point_to_linestring_dist(px, py, coords):
    """Minimum distance from point to linestring in approximate meters.

    Uses a simple equirectangular approximation — good enough at local scale.
    """
    min_dist_sq = float("inf")
    for i in range(len(coords) - 1):
        d_sq = point_to_segment_dist_sq(px, py,
                                        coords[i][0], coords[i][1],
                                        coords[i + 1][0], coords[i + 1][1])
        if d_sq < min_dist_sq:
            min_dist_sq = d_sq

    # Convert degree distance to approximate meters
    # At mid-latitudes: 1 degree ≈ 111,000 m (lat), ~85,000 m (lon at 40°)
    # Use a rough average scale factor
    return math.sqrt(min_dist_sq) * 111_000


# --- Query ---

def query_nearest_roads(ptiles_path: str, lat: float, lng: float,
                        radius_m: float = 100, max_results: int = 5,
                        check_neighbors: bool = True) -> list[dict]:
    """Find nearest roads to a GPS coordinate."""
    with open(ptiles_path, "rb") as f:
        # Read header
        header = read_header(f)

        # Read dictionary
        f.seek(header["dict_offset"])
        dict_data = f.read(header["dict_length"])

        # Read index
        f.seek(header["index_offset"])
        index_data = f.read(header["index_length"])
        index = read_index(index_data)

        # Get H3 cell(s) to search
        center_cell = h3.latlng_to_cell(lat, lng, 7)
        cells_to_check = [center_cell]
        if check_neighbors:
            cells_to_check.extend(h3.grid_disk(center_cell, 1))
            # Deduplicate (grid_disk includes center)
            cells_to_check = list(set(cells_to_check))

        # Collect all roads from relevant cells
        all_roads = []
        for cell in cells_to_check:
            cell_int = cell if isinstance(cell, int) else int(cell, 16) if isinstance(cell, str) else cell
            # h3 v4 returns int directly
            if isinstance(cell, str):
                cell_int = int(cell, 16)
            else:
                cell_int = cell

            entry = binary_search_index(index, cell_int)
            if entry is None:
                continue

            f.seek(entry["block_offset"])
            compressed = f.read(entry["block_length"])
            raw = decompress_block(compressed, dict_data)
            roads = decode_block(raw)
            all_roads.extend(roads)

    # Compute distances and sort
    results = []
    for road in all_roads:
        dist = point_to_linestring_dist(lng, lat, road["coords"])
        if dist <= radius_m:
            result = {
                "osm_id": road["osm_id"],
                "road_class": road["road_class"],
                "distance_m": round(dist, 1),
            }
            if "name" in road:
                result["name"] = road["name"]
            if "ref" in road:
                result["ref"] = road["ref"]
            if "surface" in road:
                result["surface"] = road["surface"]
            if "speed_limit_kmh" in road:
                result["speed_limit_kmh"] = road["speed_limit_kmh"]
            if "bridge_tunnel" in road:
                result["bridge_tunnel"] = road["bridge_tunnel"]
            results.append(result)

    results.sort(key=lambda r: r["distance_m"])

    # Deduplicate by osm_id (same road may appear from multiple cells)
    seen = set()
    deduped = []
    for r in results:
        if r["osm_id"] not in seen:
            seen.add(r["osm_id"])
            deduped.append(r)

    return deduped[:max_results]


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <file.roads.ptiles> <lat> <lng>")
        sys.exit(1)

    ptiles_path = sys.argv[1]
    lat = float(sys.argv[2])
    lng = float(sys.argv[3])

    print(f"Querying {ptiles_path} at ({lat}, {lng})...")
    results = query_nearest_roads(ptiles_path, lat, lng)

    if not results:
        print("No roads found within 100m")
    else:
        print(f"\nNearest {len(results)} road(s):")
        print(json.dumps(results, indent=2))
