#!/usr/bin/env python3
"""Split business_v1.parquet into per-state v2 files with bbox columns."""

import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc

PARQUET_DIR = Path("/home/aoi/kino/projects/ptiles/data/parquet")
OUT_DIR = PARQUET_DIR / "v2"


def main():
    v1_path = PARQUET_DIR / "business_v1.parquet"
    print(f"Reading {v1_path}...", flush=True)
    t0 = time.time()
    pf = pq.ParquetFile(str(v1_path))
    table = pf.read()
    print(f"  {len(table):,} rows in {time.time() - t0:.1f}s", flush=True)

    # Add bbox columns (business has lat/lon already)
    lat = table.column("lat").to_numpy()
    lon = table.column("lon").to_numpy()
    table = (
        table.append_column("lon_min", pa.array(lon, type=pa.float64()))
        .append_column("lat_min", pa.array(lat, type=pa.float64()))
        .append_column("lon_max", pa.array(lon, type=pa.float64()))
        .append_column("lat_max", pa.array(lat, type=pa.float64()))
    )

    # Get unique states
    state_arr = table.column("state_abbr").to_pylist()
    # Only process valid 2-letter US state codes
    VALID_STATES = {
        "AK",
        "AL",
        "AR",
        "AZ",
        "CA",
        "CO",
        "CT",
        "DC",
        "DE",
        "FL",
        "GA",
        "HI",
        "IA",
        "ID",
        "IL",
        "IN",
        "KS",
        "KY",
        "LA",
        "MA",
        "MD",
        "ME",
        "MI",
        "MN",
        "MO",
        "MS",
        "MT",
        "NC",
        "ND",
        "NE",
        "NH",
        "NJ",
        "NM",
        "NV",
        "NY",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VA",
        "VT",
        "WA",
        "WI",
        "WV",
        "WY",
    }
    states = sorted(set(s for s in state_arr if s in VALID_STATES))
    print(f"  {len(states)} states", flush=True)

    for state_abbr in states:
        out_path = OUT_DIR / state_abbr / "business_v2.parquet"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        mask = pc.equal(table.column("state_abbr"), state_abbr)
        tbl = table.filter(mask)

        # Sort by lat then lon to cluster geographically — makes row group
        # stats useful for bbox queries. Without this, each row group spans
        # the full state and DuckDB can't skip any on HTTP range requests.
        sort_cols = pc.sort_indices(
            tbl, sort_keys=[("lat", "ascending"), ("lon", "ascending")]
        )
        tbl = tbl.take(sort_cols)

        all_cols = [f.name for f in tbl.schema]
        pq.write_table(
            tbl,
            str(out_path),
            compression="ZSTD",
            compression_level=3,
            row_group_size=262144,
            write_statistics={col: True for col in all_cols},
        )

        sz_mb = out_path.stat().st_size / 1024 / 1024
        print(f"  {state_abbr}: {len(tbl):,} rows, {sz_mb:.0f} MB", flush=True)

    print(f"Done in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
