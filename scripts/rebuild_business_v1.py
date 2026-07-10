#!/usr/bin/env python3
"""
Rebuild business_v1.parquet from the Foursquare+Overture merged parquet,
with full column statistics and state_abbr for DuckDB row group pruning.

Reads /mnt/core/poi_lookup_us_merged.parquet (43.6M rows, 3.1 GB),
normalizes columns, writes ~home/ptiles/data/parquet/business_v1.parquet
with write_statistics on ALL columns.

Usage:
    uv run --with pyarrow --with pandas --with duckdb --with numpy python rebuild_business_v1.py
"""

import sys

sys.stdout.reconfigure(line_buffering=True)
import time
import os
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
import duckdb

SRC = "/mnt/core/poi_lookup_us_merged.parquet"
OUT = Path("/home/aoi/kino/projects/ptiles/data/parquet/business_v1.parquet")

# Target schema matching existing business_v1 conventions
SCHEMA = pa.schema(
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
    t0 = time.time()
    os.makedirs(OUT.parent, exist_ok=True)

    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    print(f"Transforming {SRC}...", flush=True)
    con.execute(f"""
        CREATE OR REPLACE TABLE biz AS
        SELECT
            CAST(ABS(hash(source_id || source)) % 9223372036854775807 AS BIGINT) AS unified_id,
            state,
            name,
            COALESCE(brand, '') AS brand,
            COALESCE(primary_category, '') AS category,
            COALESCE(phone, '') AS phone,
            COALESCE(website, '') AS website,
            COALESCE(address, '') AS address,
            lat,
            lon,
            source,
            source_id,
            CAST(ROUND(COALESCE(confidence, 1.0)) AS INTEGER) % 32767 AS confidence
        FROM read_parquet('{SRC}')
        WHERE name IS NOT NULL AND name != ''
          AND lat IS NOT NULL AND lon IS NOT NULL
          AND state IS NOT NULL
    """)

    cnt = con.execute("SELECT count(*) FROM biz").fetchone()[0]
    print(f"  Filtered: {cnt:,} rows with valid name/coords/state", flush=True)

    # Stream in batches to avoid OOM
    batch_rows = []
    total = 0
    reader = con.execute("SELECT * FROM biz").to_arrow_reader()
    for batch in reader:
        # Rename state -> state_abbr
        batch = batch.rename_columns(
            [
                "unified_id",
                "state_abbr",
                "name",
                "brand",
                "category",
                "phone",
                "website",
                "address",
                "lat",
                "lon",
                "source",
                "source_id",
                "confidence",
            ]
        )
        batch = batch.cast(SCHEMA)
        batch_rows.append(batch)
        total += batch.num_rows
        if total % 2_000_000 < 512_000:
            print(f"  Read {total:,} rows...", flush=True)

    print(f"  Total: {total:,} rows read in {time.time() - t0:.1f}s", flush=True)
    del con

    # Write with full stats
    print(f"Writing {OUT}...", flush=True)
    tbl = pa.Table.from_batches(batch_rows, schema=SCHEMA)
    del batch_rows

    all_cols = [f.name for f in SCHEMA]
    write_stats = {col: True for col in all_cols}

    pq.write_table(
        tbl,
        str(OUT),
        compression="ZSTD",
        compression_level=3,
        row_group_size=524_288,
        write_statistics=write_stats,
    )

    sz = OUT.stat().st_size
    elapsed = time.time() - t0
    print(
        f"Done! {total:,} rows, {sz / 1024 / 1024:.1f} MB in {elapsed:.1f}s", flush=True
    )

    # Verify
    pf = pq.ParquetFile(str(OUT))
    rg = pf.metadata.row_group(0)
    no_stats = []
    for j in range(rg.num_columns):
        col = rg.column(j)
        s = col.statistics
        name = ".".join(str(p) for p in col.path_in_schema)
        if not s or not s.has_min_max:
            no_stats.append(name)
    if no_stats:
        print(f"WARNING: no stats on: {no_stats}", flush=True)
    else:
        print("All columns have min/max statistics. OK.", flush=True)
    print(f"  Row groups: {pf.metadata.num_row_groups}", flush=True)


if __name__ == "__main__":
    main()
