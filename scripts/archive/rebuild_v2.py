#!/usr/bin/env python3
"""
Rebuild v2 parquet files from per-state checkpoint parquet files.

Each checkpoint file contains rows for one state (e.g.
checkpoints/TN/buildings.parquet). For WKB layers, we add
lat_min/lat_max/lon_min/lon_max bounding box columns so DuckDB can
use stats pushdown for tile-bbox range queries.

Output: data/parquet/v2/{STATE}/{layer}_v2.parquet
Plus: data/parquet/v2/manifest.json
"""

import sys
import os
import struct
import time
import json
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

import pyarrow as pa
import pyarrow.parquet as pq
import numpy as np

PARQUET_DIR = Path("/home/aoi/kino/projects/ptiles/data/parquet")
CHECKPOINT_DIR = PARQUET_DIR / "checkpoints"
OUT_DIR = PARQUET_DIR / "v2"

# WKB type constants
WKB_POINT = 1
WKB_LINESTRING = 2
WKB_POLYGON = 3
WKB_MULTIPOLYGON = 6
WKB_MULTILINESTRING = 5


def wkb_bounds_direct(wkb_bytes):
    """Extract (lon_min, lat_min, lon_max, lat_max) from raw WKB bytes.
    Uses struct.unpack_from directly on buffer for speed.
    Returns None on invalid/corrupt WKB."""
    n = len(wkb_bytes)
    if n < 9:
        return None
    try:
        byte_order = wkb_bytes[0]
        le = byte_order == 1
        gt = struct.unpack_from("<I" if le else ">I", wkb_bytes, 1)[0]

        lo_min = la_min = float("inf")
        lo_max = la_max = float("-inf")
        pos = 5

        if gt == WKB_POINT:
            lo, la = struct.unpack_from("<dd" if le else ">dd", wkb_bytes, pos)
            return (lo, la, lo, la)

        if gt in (WKB_LINESTRING, WKB_MULTILINESTRING):
            npts = struct.unpack_from("<I" if le else ">I", wkb_bytes, pos)[0]
            pos += 4
            for _ in range(npts):
                lo, la = struct.unpack_from("<dd" if le else ">dd", wkb_bytes, pos)
                pos += 16
                if lo < lo_min:
                    lo_min = lo
                if lo > lo_max:
                    lo_max = lo
                if la < la_min:
                    la_min = la
                if la > la_max:
                    la_max = la
            return (lo_min, la_min, lo_max, la_max)

        if gt == WKB_POLYGON:
            nrings = struct.unpack_from("<I" if le else ">I", wkb_bytes, pos)[0]
            pos += 4
            for _ in range(nrings):
                npts = struct.unpack_from("<I" if le else ">I", wkb_bytes, pos)[0]
                pos += 4
                for _ in range(npts):
                    lo, la = struct.unpack_from("<dd" if le else ">dd", wkb_bytes, pos)
                    pos += 16
                    if lo < lo_min:
                        lo_min = lo
                    if lo > lo_max:
                        lo_max = lo
                    if la < la_min:
                        la_min = la
                    if la > la_max:
                        la_max = la
            return (lo_min, la_min, lo_max, la_max)

        if gt == WKB_MULTIPOLYGON:
            nparts = struct.unpack_from("<I" if le else ">I", wkb_bytes, pos)[0]
            pos += 4
            for _ in range(nparts):
                sub_le = wkb_bytes[pos] == 1
                pos += 5
                nrings = struct.unpack_from("<I" if sub_le else ">I", wkb_bytes, pos)[0]
                pos += 4
                for _ in range(nrings):
                    npts = struct.unpack_from("<I" if sub_le else ">I", wkb_bytes, pos)[
                        0
                    ]
                    pos += 4
                    for _ in range(npts):
                        lo, la = struct.unpack_from(
                            "<dd" if sub_le else ">dd", wkb_bytes, pos
                        )
                        pos += 16
                        if lo < lo_min:
                            lo_min = lo
                        if lo > lo_max:
                            lo_max = lo
                        if la < la_min:
                            la_min = la
                        if la > la_max:
                            la_max = la
            return (lo_min, la_min, lo_max, la_max)

        return None
    except Exception:
        return None


LAYERS = [
    "buildings",
    "roads",
    "water",
    "parks",
    "rail",
    "places",
    "addresses",
    "business",
]

# Layers that store lat/lon as separate columns (not WKB)
POINT_LAYERS = {"places", "addresses"}


def add_bbox_column(table, has_lat_lon, lat_col=None, lon_col=None):
    """Add lat_min/lat_max/lon_min/lon_max columns."""
    if has_lat_lon:
        lat = table.column(lat_col).to_numpy()
        lon = table.column(lon_col).to_numpy()
        return (
            table.append_column("lon_min", pa.array(lon, type=pa.float64()))
            .append_column("lat_min", pa.array(lat, type=pa.float64()))
            .append_column("lon_max", pa.array(lon, type=pa.float64()))
            .append_column("lat_max", pa.array(lat, type=pa.float64()))
        )

    geom_col = table.column("geometry")
    n = len(table)

    # Direct buffer access
    arr = geom_col.combine_chunks()
    buffers = arr.buffers()
    offsets_np = np.frombuffer(buffers[1].to_pybytes(), dtype=np.int32)
    raw_data = buffers[2].to_pybytes()

    lon_min_np = np.full(n, 0.0, dtype=np.float64)
    lat_min_np = np.full(n, 0.0, dtype=np.float64)
    lon_max_np = np.full(n, 0.0, dtype=np.float64)
    lat_max_np = np.full(n, 0.0, dtype=np.float64)

    for i in range(n):
        off_start = offsets_np[i]
        off_end = offsets_np[i + 1]
        bbox = wkb_bounds_direct(raw_data[off_start:off_end])
        if bbox:
            lon_min_np[i], lat_min_np[i], lon_max_np[i], lat_max_np[i] = bbox

    return (
        table.append_column("lon_min", pa.array(lon_min_np))
        .append_column("lat_min", pa.array(lat_min_np))
        .append_column("lon_max", pa.array(lon_max_np))
        .append_column("lat_max", pa.array(lat_max_np))
    )


def process_checkpoint(layer, state_abbr):
    """Read one checkpoint parquet, add bbox, write v2 file."""
    cp_path = CHECKPOINT_DIR / state_abbr / f"{layer}.parquet"
    if not cp_path.exists():
        return 0, 0.0

    is_point = layer in POINT_LAYERS
    lat_col = "lat" if is_point else None
    lon_col = "lon" if is_point else None

    table = pq.read_table(str(cp_path))
    if len(table) == 0:
        return 0, 0.0

    table = add_bbox_column(table, is_point, lat_col, lon_col)

    # Cast binary -> large_binary for large polygons
    cols = []
    for field in table.schema:
        if pa.types.is_binary(field.type):
            cols.append(table.column(field.name).cast(pa.large_binary()))
        else:
            cols.append(table.column(field.name))
    table = pa.table(cols, schema=table.schema)

    out_dir = OUT_DIR / state_abbr
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{layer}_v2.parquet"

    all_cols = [f.name for f in table.schema]
    pq.write_table(
        table,
        str(out_path),
        compression="ZSTD",
        compression_level=3,
        row_group_size=262144,
        write_statistics={col: True for col in all_cols},
    )

    nrows = len(table)
    sz_mb = out_path.stat().st_size / 1024 / 1024
    return nrows, sz_mb


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    states = sorted(
        d.name
        for d in CHECKPOINT_DIR.iterdir()
        if d.is_dir() and d.name != "__pycache__"
    )

    print(f"{len(states)} states, {len(LAYERS)} layers")
    print()

    layer_totals = {l: {"rows": 0, "mb": 0, "states": 0} for l in LAYERS}
    all_state_sizes = {}

    for state_abbr in states:
        print(f"  {state_abbr}...", end="", flush=True)
        t0 = time.time()
        state_sizes = {}
        for layer in LAYERS:
            nrows, sz_mb = process_checkpoint(layer, state_abbr)
            if nrows > 0:
                layer_totals[layer]["rows"] += nrows
                layer_totals[layer]["mb"] += sz_mb
                layer_totals[layer]["states"] += 1
                if state_abbr not in all_state_sizes:
                    all_state_sizes[state_abbr] = {}
                all_state_sizes[state_abbr][layer] = round(sz_mb, 1)

        elapsed = time.time() - t0
        print(f" {elapsed:.1f}s")

    # Write manifest
    manifest = {
        "version": 2,
        "base_url": "https://maps.mydatatimeline.com/earth/v2",
        "layers": {},
    }
    for layer in LAYERS:
        info = layer_totals[layer]
        # Collect which states have this layer
        states_with = sorted(
            s for s, layers in all_state_sizes.items() if layer in layers
        )
        manifest["layers"][layer] = {
            "states": states_with,
            "total_mb": round(info["mb"], 1),
            "total_rows": info["rows"],
        }

    manifest_path = OUT_DIR / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n{'=' * 60}")
    print(f"Done! Manifest: {manifest_path}")
    for layer, info in manifest["layers"].items():
        print(
            f"  {layer}: {info['total_rows']:,} rows, {info['total_mb']:.0f} MB, "
            f"{len(info['states'])} states"
        )
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
