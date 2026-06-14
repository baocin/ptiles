#!/usr/bin/env python3
"""
Build US.admin.ptiles — admin boundaries + ZIP codes + time zones.

Uses a pre-computed H3 lookup grid: for every H3 resolution 7 cell that
intersects US land, store which state, county, ZIP, and timezone it belongs to.

Usage:
    python build_admin.py /Volumes/core/timeline-ptiles-cache/admin_data/ \
                          /Volumes/core/timeline-ptiles-cache/tiles/US.admin.ptiles

Required data in admin_data/:
    states/cb_2023_us_state_500k.shp
    counties/cb_2023_us_county_500k.shp
    zcta/cb_2020_us_zcta520_500k.shp
    tz/combined.json
"""

import sys
import os
import struct
import time
import json
import pickle
from collections import defaultdict

import geopandas as gpd
import h3
import numpy as np
from shapely.geometry import Point, shape
from shapely.prepared import prep
from shapely.strtree import STRtree

sys.path.insert(0, os.path.dirname(__file__))
from shared import (
    write_header, HEADER_SIZE,
    encode_varint, compress_block, train_dictionary,
)

import zstandard as zstd


def load_boundaries(admin_data_dir: str):
    """Load all boundary datasets."""
    print("Loading boundary datasets...")

    states = gpd.read_file(os.path.join(admin_data_dir, "states", "cb_2023_us_state_500k.shp"))
    print(f"  States: {len(states)}")

    counties = gpd.read_file(os.path.join(admin_data_dir, "counties", "cb_2023_us_county_500k.shp"))
    print(f"  Counties: {len(counties)}")

    zcta = gpd.read_file(os.path.join(admin_data_dir, "zcta", "cb_2020_us_zcta520_500k.shp"))
    print(f"  ZCTAs: {len(zcta)}")

    tz = gpd.read_file(os.path.join(admin_data_dir, "tz", "combined.json"))
    # Filter to relevant timezones (clip to US bbox)
    us_bbox = (-180, 17, -65, 72)  # generous US bounds including AK/HI/PR
    tz = tz.cx[us_bbox[0]:us_bbox[2], us_bbox[1]:us_bbox[3]]
    print(f"  Time zones (US-clipped): {len(tz)}")

    return states, counties, zcta, tz


def build_string_tables(states, counties, zcta, tz):
    """Build indexed string tables for compact storage."""
    # Country table (just US for now)
    country_table = ["United States"]

    # State table - indexed by STATEFP
    state_names = {}
    for _, row in states.iterrows():
        state_names[row["STATEFP"]] = row["NAME"]
    # Build ordered list, map FIPS -> index
    state_table = sorted(set(state_names.values()))
    state_name_to_idx = {name: i for i, name in enumerate(state_table)}
    state_fips_to_idx = {fips: state_name_to_idx[name] for fips, name in state_names.items()}

    # County table - indexed by STATEFP+COUNTYFP
    county_names = {}
    for _, row in counties.iterrows():
        key = row["STATEFP"] + row["COUNTYFP"]
        county_names[key] = row["NAME"]
    county_table = sorted(set(county_names.values()))
    county_name_to_idx = {name: i for i, name in enumerate(county_table)}
    county_fips_to_idx = {fips: county_name_to_idx[name] for fips, name in county_names.items()}

    # ZIP table
    zip_table = sorted(zcta["ZCTA5CE20"].unique().tolist())
    zip_to_idx = {z: i for i, z in enumerate(zip_table)}

    # Timezone table
    tz_table = sorted(tz["tzid"].unique().tolist())
    tz_to_idx = {t: i for i, t in enumerate(tz_table)}

    print(f"  String tables: {len(state_table)} states, {len(county_table)} counties, "
          f"{len(zip_table)} ZIPs, {len(tz_table)} timezones")

    return {
        "country": country_table,
        "state": state_table,
        "state_fips_to_idx": state_fips_to_idx,
        "county": county_table,
        "county_fips_to_idx": county_fips_to_idx,
        "zip": zip_table,
        "zip_to_idx": zip_to_idx,
        "tz": tz_table,
        "tz_to_idx": tz_to_idx,
    }


def build_spatial_indexes(states, counties, zcta, tz):
    """Build spatial indexes for fast point-in-polygon queries."""
    print("Building spatial indexes...")

    def make_index(gdf, key_col):
        geoms = gdf.geometry.values
        keys = gdf[key_col].values
        tree = STRtree(geoms)
        prepped = [(prep(g), k) for g, k in zip(geoms, keys)]
        return tree, prepped, geoms, keys

    state_tree = STRtree(states.geometry.values)
    county_tree = STRtree(counties.geometry.values)
    zcta_tree = STRtree(zcta.geometry.values)
    tz_tree = STRtree(tz.geometry.values)

    return state_tree, county_tree, zcta_tree, tz_tree


def query_point(lat, lng, states, counties, zcta, tz,
                state_tree, county_tree, zcta_tree, tz_tree):
    """Find state/county/zip/tz for a point."""
    pt = Point(lng, lat)

    result = {
        "state_fips": None,
        "county_fips": None,
        "zip": None,
        "tz": None,
    }

    # State
    idxs = state_tree.query(pt)
    for i in idxs:
        if states.geometry.iloc[i].contains(pt):
            result["state_fips"] = states.iloc[i]["STATEFP"]
            break

    # County
    idxs = county_tree.query(pt)
    for i in idxs:
        if counties.geometry.iloc[i].contains(pt):
            row = counties.iloc[i]
            result["county_fips"] = row["STATEFP"] + row["COUNTYFP"]
            break

    # ZIP
    idxs = zcta_tree.query(pt)
    for i in idxs:
        if zcta.geometry.iloc[i].contains(pt):
            result["zip"] = zcta.iloc[i]["ZCTA5CE20"]
            break

    # Timezone
    idxs = tz_tree.query(pt)
    for i in idxs:
        if tz.geometry.iloc[i].contains(pt):
            result["tz"] = tz.iloc[i]["tzid"]
            break

    return result


def collect_h3_cells(states):
    """Get all H3 resolution 7 cells that cover US land."""
    print("Collecting H3 cells covering US land...")
    all_cells = set()

    for idx, row in states.iterrows():
        name = row["NAME"]
        geom = row.geometry
        # Use h3.polygon_to_cells for each polygon/multipolygon
        if geom.geom_type == "Polygon":
            polys = [geom]
        elif geom.geom_type == "MultiPolygon":
            polys = list(geom.geoms)
        else:
            continue

        state_cells = set()
        for poly in polys:
            try:
                outer = list(poly.exterior.coords)
                # h3 expects (lat, lng) tuples
                h3_poly = h3.LatLngPoly([(lat, lng) for lng, lat in outer])
                cells = h3.polygon_to_cells(h3_poly, 7)
                state_cells.update(cells)
            except Exception as e:
                print(f"  Warning: {name} polygon failed: {e}")
                continue

        all_cells.update(state_cells)
        print(f"  {name}: {len(state_cells):,} cells (total: {len(all_cells):,})")

    print(f"  Total H3 cells: {len(all_cells):,}")
    return all_cells


def build_lookup_grid(cells, states, counties, zcta, tz,
                      state_tree, county_tree, zcta_tree, tz_tree,
                      string_tables, cache_path=None):
    """Build the H3 lookup grid by querying each cell's center point."""

    if cache_path and os.path.exists(cache_path):
        print(f"Loading cached lookup grid from {cache_path}...")
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    print(f"Building lookup grid for {len(cells):,} cells...")
    grid = []
    done = 0
    t0 = time.time()

    sorted_cells = sorted(cells)

    for cell in sorted_cells:
        lat, lng = h3.cell_to_latlng(cell)
        result = query_point(lat, lng, states, counties, zcta, tz,
                             state_tree, county_tree, zcta_tree, tz_tree)

        # Map to indices
        state_idx = string_tables["state_fips_to_idx"].get(result["state_fips"], 255)
        county_idx = string_tables["county_fips_to_idx"].get(result["county_fips"], 65535)
        zip_idx = string_tables["zip_to_idx"].get(result["zip"], 65535)
        tz_idx = string_tables["tz_to_idx"].get(result["tz"], 255)

        cell_int = cell if isinstance(cell, int) else int(cell, 16)

        grid.append({
            "h3_cell": cell_int,
            "country_idx": 0,  # US
            "state_idx": state_idx,
            "county_idx": county_idx,
            "zip_idx": zip_idx,
            "tz_idx": tz_idx,
            "boundary_flags": 0,  # TODO: detect boundary cells
        })

        done += 1
        if done % 10_000 == 0:
            elapsed = time.time() - t0
            rate = done / elapsed
            eta = (len(sorted_cells) - done) / rate
            print(f"  {done:,}/{len(sorted_cells):,} cells ({rate:.0f}/s, ETA {eta/60:.1f}m)")

    # Sort by h3_cell for binary search
    grid.sort(key=lambda e: e["h3_cell"])

    if cache_path:
        print(f"Caching lookup grid to {cache_path}...")
        with open(cache_path, "wb") as f:
            pickle.dump(grid, f, protocol=5)

    return grid


def encode_string_table(strings: list[str]) -> bytes:
    """Encode a string table as null-terminated UTF-8 strings."""
    buf = bytearray()
    buf.extend(struct.pack("<I", len(strings)))
    for s in strings:
        encoded = s.encode("utf-8")
        buf.extend(struct.pack("<H", len(encoded)))
        buf.extend(encoded)
    return bytes(buf)


def encode_lookup_grid(grid: list[dict]) -> bytes:
    """Encode the H3 lookup grid."""
    buf = bytearray()
    buf.extend(struct.pack("<I", len(grid)))
    for entry in grid:
        buf.extend(struct.pack("<Q", entry["h3_cell"]))       # 8 bytes
        buf.append(entry["country_idx"])                        # 1 byte
        buf.append(entry["state_idx"])                          # 1 byte
        buf.extend(struct.pack("<H", entry["county_idx"]))     # 2 bytes
        buf.extend(struct.pack("<H", entry["zip_idx"]))        # 2 bytes
        buf.append(entry["tz_idx"])                             # 1 byte
        buf.append(entry["boundary_flags"])                     # 1 byte
    return bytes(buf)


def simplify_coords(coords, tolerance=0.0005):
    """Douglas-Peucker simplification (~50m at mid-latitudes)."""
    try:
        from shapely.geometry import LineString
        line = LineString(coords)
        simplified = line.simplify(tolerance, preserve_topology=True)
        return list(simplified.coords)
    except Exception:
        return coords


def encode_boundary_polygons(states, counties, string_tables) -> bytes:
    """Encode admin boundary polygons for rendering/PIP fallback."""
    buf = bytearray()

    all_polys = []

    # State polygons (all rings of MultiPolygon)
    for _, row in states.iterrows():
        geom = row.geometry
        name = row["NAME"]
        state_idx = string_tables["state_fips_to_idx"].get(row["STATEFP"], 255)

        if geom.geom_type == "Polygon":
            polys = [geom]
        elif geom.geom_type == "MultiPolygon":
            polys = list(geom.geoms)
        else:
            continue

        for poly in polys:
            coords = simplify_coords(list(poly.exterior.coords))
            if len(coords) >= 3:
                all_polys.append({
                    "state_idx": state_idx,
                    "name": name,
                    "coords": coords,
                })

    # County polygons (simplified more aggressively)
    for _, row in counties.iterrows():
        geom = row.geometry
        name = row["NAME"]
        county_key = row["STATEFP"] + row["COUNTYFP"]
        # Use state_idx 255 to mark county-level (admin_level distinction)
        state_idx = string_tables["state_fips_to_idx"].get(row["STATEFP"], 255)

        if geom.geom_type == "Polygon":
            polys = [geom]
        elif geom.geom_type == "MultiPolygon":
            polys = list(geom.geoms)
        else:
            continue

        for poly in polys:
            coords = simplify_coords(list(poly.exterior.coords), tolerance=0.001)
            if len(coords) >= 3:
                all_polys.append({
                    "state_idx": state_idx,
                    "name": f"{name} County",
                    "coords": coords,
                })

    buf.extend(struct.pack("<I", len(all_polys)))
    for poly in all_polys:
        buf.append(poly["state_idx"])
        name_bytes = poly["name"].encode("utf-8")
        buf.extend(struct.pack("<H", len(name_bytes)))
        buf.extend(name_bytes)
        buf.extend(struct.pack("<I", len(poly["coords"])))
        for lon, lat in poly["coords"]:
            buf.extend(struct.pack("<ii", round(lon * 100_000), round(lat * 100_000)))

    print(f"  Encoded {len(all_polys)} boundary polygons (states + counties)")
    return bytes(buf)


def build_admin_ptiles(admin_data_dir: str, output_path: str):
    """Full pipeline: shapefiles → admin.ptiles."""
    t0 = time.time()

    # Load data
    states, counties, zcta, tz = load_boundaries(admin_data_dir)
    string_tables = build_string_tables(states, counties, zcta, tz)

    # Build spatial indexes
    state_tree, county_tree, zcta_tree, tz_tree = build_spatial_indexes(
        states, counties, zcta, tz)

    # Collect H3 cells
    cache_dir = os.path.join(admin_data_dir, "cache")
    os.makedirs(cache_dir, exist_ok=True)

    cells_cache = os.path.join(cache_dir, "h3_cells.pkl")
    if os.path.exists(cells_cache):
        print(f"Loading cached H3 cells...")
        with open(cells_cache, "rb") as f:
            all_cells = pickle.load(f)
        print(f"  {len(all_cells):,} cells")
    else:
        all_cells = collect_h3_cells(states)
        with open(cells_cache, "wb") as f:
            pickle.dump(all_cells, f)

    # Build lookup grid
    grid_cache = os.path.join(cache_dir, "lookup_grid.pkl")
    grid = build_lookup_grid(
        all_cells, states, counties, zcta, tz,
        state_tree, county_tree, zcta_tree, tz_tree,
        string_tables, cache_path=grid_cache)

    # Encode components
    print("Encoding components...")

    # String tables (combined into one blob)
    st_buf = bytearray()
    st_buf.extend(encode_string_table(string_tables["country"]))
    st_buf.extend(encode_string_table(string_tables["state"]))
    st_buf.extend(encode_string_table(string_tables["county"]))
    st_buf.extend(encode_string_table(string_tables["zip"]))
    st_buf.extend(encode_string_table(string_tables["tz"]))
    string_table_data = bytes(st_buf)

    # Boundary polygons
    polygon_data = encode_boundary_polygons(states, counties, string_tables)

    # Lookup grid
    grid_data = encode_lookup_grid(grid)

    # Compress string tables and polygons with zstd (no dictionary needed, small data)
    cctx = zstd.ZstdCompressor(level=19)
    string_table_compressed = cctx.compress(string_table_data)
    polygon_compressed = cctx.compress(polygon_data)
    # Grid is NOT compressed — needs binary searchable random access

    print(f"  String tables: {len(string_table_data):,} → {len(string_table_compressed):,} bytes")
    print(f"  Polygons: {len(polygon_data):,} → {len(polygon_compressed):,} bytes")
    print(f"  Lookup grid: {len(grid_data):,} bytes ({len(grid):,} entries × 16 bytes)")

    # Layout:
    # Header (256) → String tables (compressed) → Polygons (compressed) → Lookup grid (aux)
    # We use dict_offset/dict_length for string tables
    # index_offset/index_length for polygons
    # aux_offset/aux_length for the lookup grid

    dict_offset = HEADER_SIZE
    dict_length = len(string_table_compressed)
    index_offset = dict_offset + dict_length
    index_length = len(polygon_compressed)
    blocks_offset = index_offset + index_length  # not used for this layer
    aux_offset = blocks_offset
    aux_length = len(grid_data)

    # Bounding box from grid
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")
    for entry in grid[::100]:  # Sample every 100th for speed
        cell_id = entry["h3_cell"]
        if isinstance(cell_id, int):
            cell_id = hex(cell_id)
        lat, lng = h3.cell_to_latlng(cell_id)
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
        min_lon = min(min_lon, lng)
        max_lon = max(max_lon, lng)

    # Write file
    print("Writing output file...")
    with open(output_path, "wb") as f:
        write_header(
            f,
            magic=b"PTILESA",
            version=1,
            min_lat=min_lat, min_lon=min_lon,
            max_lat=max_lat, max_lon=max_lon,
            feature_count=len(grid),
            block_count=0,
            dict_offset=dict_offset, dict_length=dict_length,
            index_offset=index_offset, index_length=index_length,
            blocks_offset=blocks_offset,
            aux_offset=aux_offset, aux_length=aux_length,
        )
        f.write(string_table_compressed)
        f.write(polygon_compressed)
        f.write(grid_data)

    elapsed = time.time() - t0
    file_size = os.path.getsize(output_path)
    print(f"\nDone in {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"  Output: {output_path}")
    print(f"  File size: {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MB)")
    print(f"  Grid entries: {len(grid):,}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <admin_data_dir> <output.admin.ptiles>")
        sys.exit(1)

    build_admin_ptiles(sys.argv[1], sys.argv[2])
