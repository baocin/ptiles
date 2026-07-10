#!/usr/bin/env python3
"""
Merge all checkpoint state parquets into national files.

Usage:
  uv run --with pyarrow --with pandas --with numpy python merge_parquet_checkpoints.py
"""

import sys
import os
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

sys.path.insert(0, os.path.dirname(__file__))
import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

OUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/parquet")
CHECKPOINT_DIR = OUT_DIR / "checkpoints"

# Schemas (copied from build_national_parquet.py)
SCHEMA_ROADS = pa.schema(
    [
        pa.field("osm_id", pa.int64()),
        pa.field("state_abbr", pa.string()),
        pa.field("highway", pa.string()),
        pa.field("name", pa.string()),
        pa.field("ref", pa.string()),
        pa.field("oneway", pa.bool_()),
        pa.field("maxspeed", pa.int32()),
        pa.field("lanes", pa.int16()),
        pa.field("surface", pa.string()),
        pa.field("bridge", pa.bool_()),
        pa.field("tunnel", pa.bool_()),
        pa.field("geometry", pa.binary()),
    ]
)

SCHEMA_WATER = pa.schema(
    [
        pa.field("osm_id", pa.int64()),
        pa.field("state_abbr", pa.string()),
        pa.field("water_type", pa.string()),
        pa.field("name", pa.string()),
        pa.field("width", pa.int32()),
        pa.field("geometry", pa.binary()),
    ]
)

SCHEMA_BUILDINGS = pa.schema(
    [
        pa.field("osm_id", pa.int64()),
        pa.field("state_abbr", pa.string()),
        pa.field("btype", pa.string()),
        pa.field("name", pa.string()),
        pa.field("height_m", pa.float32()),
        pa.field("geometry", pa.binary()),
    ]
)

SCHEMA_PARKS = pa.schema(
    [
        pa.field("osm_id", pa.int64()),
        pa.field("state_abbr", pa.string()),
        pa.field("park_type", pa.string()),
        pa.field("name", pa.string()),
        pa.field("geometry", pa.binary()),
    ]
)

SCHEMA_RAIL = pa.schema(
    [
        pa.field("osm_id", pa.int64()),
        pa.field("state_abbr", pa.string()),
        pa.field("rail_type", pa.string()),
        pa.field("geom_type", pa.string()),
        pa.field("name", pa.string()),
        pa.field("geometry", pa.binary()),
    ]
)

SCHEMA_PLACES = pa.schema(
    [
        pa.field("osm_id", pa.int64()),
        pa.field("state_abbr", pa.string()),
        pa.field("place_type", pa.string()),
        pa.field("name", pa.string()),
        pa.field("alt_name", pa.string()),
        pa.field("population", pa.int32()),
        pa.field("admin_level", pa.int16()),
        pa.field("lat", pa.float64()),
        pa.field("lon", pa.float64()),
    ]
)

SCHEMA_ADDRESS = pa.schema(
    [
        pa.field("osm_id", pa.int64()),
        pa.field("state_abbr", pa.string()),
        pa.field("street", pa.string()),
        pa.field("housenumber", pa.string()),
        pa.field("city", pa.string()),
        pa.field("postcode", pa.string()),
        pa.field("lat", pa.float64()),
        pa.field("lon", pa.float64()),
    ]
)

SCHEMA_BUSINESS = pa.schema(
    [
        pa.field("unified_id", pa.int64()),
        pa.field("state_abbr", pa.string()),
        pa.field("name", pa.string()),
        pa.field("brand", pa.string()),
        pa.field("category", pa.string()),
        pa.field("phone", pa.string()),
        pa.field("website", pa.string()),
        pa.field("address", pa.string()),
        pa.field("lat", pa.float64()),
        pa.field("lon", pa.float64()),
        pa.field("source", pa.string()),
        pa.field("source_id", pa.string()),
        pa.field("confidence", pa.int16()),
    ]
)


def main():
    layer_schemas = {
        "roads": SCHEMA_ROADS,
        "water": SCHEMA_WATER,
        "buildings": SCHEMA_BUILDINGS,
        "parks": SCHEMA_PARKS,
        "rail": SCHEMA_RAIL,
        "places": SCHEMA_PLACES,
        "addresses": SCHEMA_ADDRESS,
    }

    out_dir = OUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    # Discover all state checkpoint dirs with full 7-layer coverage
    state_dirs = sorted(
        [
            d
            for d in CHECKPOINT_DIR.iterdir()
            if d.is_dir()
            and d.name != "__pycache__"
            and all((d / f"{l}.parquet").exists() for l in layer_schemas)
        ]
    )
    print(
        f"Found {len(state_dirs)} complete state checkpoints to merge: {', '.join(d.name for d in state_dirs)}",
        flush=True,
    )

    all_layers = list(layer_schemas.keys())

    for layer in all_layers:
        t1 = time.time()
        all_dfs = []
        for state_dir in state_dirs:
            cp = state_dir / f"{layer}.parquet"
            if cp.exists():
                sz = cp.stat().st_size
                if sz == 0:
                    print(
                        f"  Skipping {state_dir.name}/{layer}: empty file", flush=True
                    )
                    continue
                df = pd.read_parquet(str(cp))
                if len(df) > 0:
                    all_dfs.append(df)
        if all_dfs:
            full_df = pd.concat(all_dfs, ignore_index=True)
            table = pa.Table.from_pandas(
                full_df, schema=layer_schemas[layer], preserve_index=False
            )
            out_path = out_dir / f"{layer}_v1.parquet"
            # Enable statistics on ALL columns for predicate pushdown
            all_cols = [f.name for f in layer_schemas[layer]]
            write_statistics = {col: True for col in all_cols}

            pq.write_table(
                table,
                str(out_path),
                compression="ZSTD",
                compression_level=3,
                row_group_size=524288,
                write_statistics=write_statistics,
            )
            sz = out_path.stat().st_size
            print(
                f"  {layer}: {len(full_df):,} rows, {sz / 1024 / 1024:.1f} MB in {time.time() - t1:.1f}s",
                flush=True,
            )
            del full_df, table, all_dfs
        else:
            print(f"  {layer}: no data found", flush=True)
        gc_collect()

    # Rebuild business_v1.parquet from existing business.parquet with proper stats
    biz_src = out_dir / "business.parquet"
    biz_out = out_dir / "business_v1.parquet"
    if biz_src.exists() and not biz_out.exists():
        t1 = time.time()
        print("\n  Rebuilding business_v1.parquet from business.parquet...", flush=True)
        # Read in batches to avoid OOM on NAS
        biz_pf = pq.ParquetFile(str(biz_src))
        total = 0
        batch_rows = []
        for batch in biz_pf.iter_batches(batch_size=262144):
            batch_rows.append(batch)
            total += batch.num_rows
        if batch_rows:
            biz_tbl = pa.Table.from_batches(batch_rows)
            all_cols = [f.name for f in biz_tbl.schema]
            write_stats = {col: True for col in all_cols}
            pq.write_table(
                biz_tbl,
                str(biz_out),
                compression="ZSTD",
                compression_level=3,
                row_group_size=524288,
                write_statistics=write_stats,
            )
            sz = biz_out.stat().st_size
            el = time.time() - t1
            print(
                f"  business_v1: {total:,} rows, {sz / 1024 / 1024:.1f} MB in {el:.1f}s",
                flush=True,
            )
    elif biz_out.exists():
        sz = biz_out.stat().st_size
        print(
            f"\n  business_v1.parquet already exists: {sz / 1024 / 1024:.1f} MB (skipped)",
            flush=True,
        )
    else:
        print("\n  business.parquet not found, skipping business rebuild", flush=True)

    print(f"\nDone! Files in {out_dir}", flush=True)
    for p in sorted(out_dir.glob("*.parquet")):
        print(f"  {p.name}: {p.stat().st_size / 1024 / 1024:.1f} MB", flush=True)


def gc_collect():
    import gc

    gc.collect()


if __name__ == "__main__":
    main()
