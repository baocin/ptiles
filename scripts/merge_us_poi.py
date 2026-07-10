#!/usr/bin/env python3
"""
Merge Foursquare + Overture Places into a nationwide POI parquet.

Uses DuckDB for streaming merge (handles memory better than pyarrow
for 100M+ rows). Filters to US, deduplicates cross-source.

Output: /mnt/core/poi_lookup_us.parquet

Usage:
    uv run --with duckdb python3 merge_us_poi.py
"""

import sys

sys.stdout.reconfigure(line_buffering=True)
import os
import time
import glob
import duckdb

FOURSQUARE_PATH = "/mnt/core/foursquare_places.parquet"
OVERTURE_DIR = "/mnt/core/overture-places/"
OUTPUT_PATH = "/mnt/core/poi_lookup_us.parquet"

# US bounding box (conterminous + AK/HI)
US_BBOX = (24.0, -125.0, 50.0, -66.0)


def main():
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=4")
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")

    print("=" * 60, flush=True)
    print("STEP 1: Load & filter Overture Places", flush=True)
    print("=" * 60, flush=True)

    overture_files = sorted(glob.glob(os.path.join(OVERTURE_DIR, "*.zstd.parquet")))
    print(f"  Files: {len(overture_files)}", flush=True)

    # Create a view over Overture files filtered to US
    # Overture stores addresses[0].country in a struct
    con.execute(f"""
        CREATE OR REPLACE VIEW overture_us AS
        SELECT
            'overture' AS source,
            id AS source_id,
            names['primary']::VARCHAR AS name,
            ST_X(geometry::GEOMETRY) AS lon,
            ST_Y(geometry::GEOMETRY) AS lat,
            categories['primary']::VARCHAR AS primary_category,
            confidence,
            addresses[1].freeform::VARCHAR AS address,
            addresses[1].locality::VARCHAR AS city,
            addresses[1].region::VARCHAR AS state,
            addresses[1].country::VARCHAR AS country,
            phones[1]::VARCHAR AS phone,
            websites[1]::VARCHAR AS website,
            emails[1]::VARCHAR AS email,
            socials[1]::VARCHAR AS social,
            operating_status,
            brand['names']['primary']::VARCHAR AS brand
        FROM read_parquet({overture_files!r})
        WHERE addresses[1].country = 'US'
           OR (ST_Y(geometry::GEOMETRY) BETWEEN {US_BBOX[0]} AND {US_BBOX[2]}
               AND ST_X(geometry::GEOMETRY) BETWEEN {US_BBOX[1]} AND {US_BBOX[3]})
    """)

    ov_count = con.execute("SELECT count(*) FROM overture_us").fetchone()[0]
    print(f"  Overture US records: {ov_count:,}", flush=True)

    print("\n" + "=" * 60, flush=True)
    print("STEP 2: Load & filter Foursquare Places", flush=True)
    print("=" * 60, flush=True)

    con.execute(f"""
    CREATE OR REPLACE VIEW foursquare_us AS
    SELECT
        'foursquare' AS source,
        fsq_place_id AS source_id,
        name,
        longitude AS lon,
        latitude AS lat,
        fsq_category_labels[1]::VARCHAR AS primary_category,
        1.0 AS confidence,
        address,
        locality AS city,
        region AS state,
        'US' AS country,
        tel AS phone,
        website,
        email,
        NULL AS social,
        CASE WHEN date_closed IS NOT NULL AND date_closed != '' THEN 'permanently_closed' ELSE '' END AS operating_status,
        NULL AS brand
    FROM read_parquet('{FOURSQUARE_PATH}')
    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
      AND latitude BETWEEN {US_BBOX[0]} AND {US_BBOX[2]}
      AND longitude BETWEEN {US_BBOX[1]} AND {US_BBOX[3]}
    """)

    fs_count = con.execute("SELECT count(*) FROM foursquare_us").fetchone()[0]
    print(f"  Foursquare US records: {fs_count:,}", flush=True)

    print("\n" + "=" * 60, flush=True)
    print("STEP 3: Combine (no cross-source dedup yet)", flush=True)
    print("=" * 60, flush=True)

    combined_count = con.execute("""
        SELECT count(*) FROM (
            SELECT * FROM foursquare_us
            UNION ALL
            SELECT * FROM overture_us
        )
    """).fetchone()[0]
    print(f"  Combined: {combined_count:,}", flush=True)

    print("\n" + "=" * 60, flush=True)
    print("STEP 4: Write merged parquet (append mode)", flush=True)
    print("=" * 60, flush=True)

    # Write Foursquare first, then append Overture
    con.execute("""
        COPY (
            SELECT * FROM foursquare_us
        ) TO '/mnt/core/poi_lookup_us.parquet'
        (FORMAT PARQUET, CODEC 'ZSTD', ROW_GROUP_SIZE 2000000)
    """)
    print(f"  Foursquare written ({time.time() - t0:.1f}s)", flush=True)

    con.execute("""
        COPY (
            SELECT * FROM overture_us
        ) TO '/mnt/core/poi_lookup_overture.parquet'
        (FORMAT PARQUET, CODEC 'ZSTD', ROW_GROUP_SIZE 2000000)
    """)
    print(f"  Overture written ({time.time() - t0:.1f}s)", flush=True)

    # Concatenate
    con.execute("""
        COPY (
            SELECT * FROM '/mnt/core/poi_lookup_us.parquet'
            UNION ALL
            SELECT * FROM '/mnt/core/poi_lookup_overture.parquet'
        ) TO '/mnt/core/poi_lookup_us_merged.parquet'
        (FORMAT PARQUET, CODEC 'ZSTD', ROW_GROUP_SIZE 2000000)
    """)
    print(f"  Merged written ({time.time() - t0:.1f}s)", flush=True)

    sz = os.path.getsize("/mnt/core/poi_lookup_us_merged.parquet")
    print("\nOutput: /mnt/core/poi_lookup_us_merged.parquet", flush=True)
    print(f"  Size: {sz / 1024 / 1024 / 1024:.2f} GB", flush=True)
    print(f"  Total time: {time.time() - t0:.1f}s", flush=True)

    con.close()


if __name__ == "__main__":
    main()
