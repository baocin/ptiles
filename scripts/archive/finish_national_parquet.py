#!/usr/bin/env python3
"""
Finish the national parquet dataset by building AK + HI checkpoints
and merging them into the existing per-layer parquet files.

Usage:
    uv run --with osmium --with pyarrow --with pandas --with shapely --with h3 --with zstandard \
        python scripts/finish_national_parquet.py

This does NOT re-extract business data (already national).
Does NOT re-merge existing 49 states (just appends AK + HI).
"""

import sys
import os
import time
import json
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.stdout.reconfigure(line_buffering=True)

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

# Reuse the extractor from build_national_parquet
from build_national_parquet import (
    ensure_pbf,
    build_state_rows,
    SCHEMA_ROADS,
    SCHEMA_WATER,
    SCHEMA_BUILDINGS,
    SCHEMA_PARKS,
    SCHEMA_RAIL,
    SCHEMA_PLACES,
    SCHEMA_ADDRESS,
)

OUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/parquet")
CHECKPOINT_DIR = OUT_DIR / "checkpoints"
states_to_process = ["AK", "HI"]


def main():
    print("=== Finishing national parquet: AK + HI ===", flush=True)
    t0 = time.time()

    # --- Step 1: process AK and HI from PBFs ---
    for abbr in states_to_process:
        cp_dir = CHECKPOINT_DIR / abbr
        if cp_dir.exists():
            # Check if all 7 layers exist
            all_layers = [
                "roads",
                "water",
                "buildings",
                "parks",
                "rail",
                "places",
                "addresses",
            ]
            if all((cp_dir / f"{l}.parquet").exists() for l in all_layers):
                print(
                    f"  {abbr}: checkpoint already exists, skipping extraction",
                    flush=True,
                )
                continue

        pbf_path = ensure_pbf(abbr)
        print(f"\n  Processing {abbr} from {pbf_path}...", flush=True)
        t1 = time.time()
        build_state_rows(abbr, pbf_path)
        print(f"  {abbr} done in {time.time() - t1:.0f}s", flush=True)

        # Update completed_states.json
        csf = CHECKPOINT_DIR / "completed_states.json"
        if csf.exists():
            with open(csf) as f:
                completed = set(json.load(f))
        else:
            completed = set()
        completed.add(abbr)
        with open(csf, "w") as f:
            json.dump(list(completed), f)

    # --- Step 2: merge AK + HI into existing national per-layer parquets ---
    print("\n  Merging AK + HI into existing national files...", flush=True)

    layers = [
        ("roads", SCHEMA_ROADS),
        ("water", SCHEMA_WATER),
        ("buildings", SCHEMA_BUILDINGS),
        ("parks", SCHEMA_PARKS),
        ("rail", SCHEMA_RAIL),
        ("places", SCHEMA_PLACES),
        ("addresses", SCHEMA_ADDRESS),
    ]

    for layer_name, schema in layers:
        t1 = time.time()
        new_rows = []
        for abbr in states_to_process:
            cp = CHECKPOINT_DIR / abbr / f"{layer_name}.parquet"
            if cp.exists() and cp.stat().st_size > 0:
                df = pd.read_parquet(str(cp))
                if len(df) > 0:
                    new_rows.append(df)
                    print(f"    {abbr}/{layer_name}: {len(df):,} rows", flush=True)

        if not new_rows:
            print(f"    {layer_name}: no new data", flush=True)
            continue

        new_df = pd.concat(new_rows, ignore_index=True)
        new_table = pa.Table.from_pandas(new_df, schema=schema, preserve_index=False)

        out_path = OUT_DIR / f"{layer_name}.parquet"
        if out_path.exists():
            # Read existing, concat, write back
            existing = pq.read_table(str(out_path))
            combined = pa.concat_tables([existing, new_table])
            pq.write_table(combined, str(out_path))
            total_rows = combined.num_rows
        else:
            pq.write_table(new_table, str(out_path))
            total_rows = new_table.num_rows

        sz = out_path.stat().st_size
        print(
            f"    {layer_name}: +{len(new_df):,} rows → {total_rows:,} total, "
            f"{sz / 1024 / 1024:.1f} MB in {time.time() - t1:.1f}s",
            flush=True,
        )

    print(f"\n=== Done in {time.time() - t0:.0f}s ===", flush=True)
    print("National parquet files in", OUT_DIR, flush=True)
    for p in sorted(OUT_DIR.glob("*.parquet")):
        print(f"  {p.name}: {p.stat().st_size / 1024 / 1024:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
