#!/usr/bin/env python3
"""Build the per-state POI extracts that build_full_ptilesb.py reads.

This stage had no script in the repo: build_full_ptilesb.py reads
/mnt/core/poi_state_extracts/{ST}.parquet plus a chain index and a brand map,
and nothing produced any of the three -- they were made ad hoc and then deleted,
so the business layer could not be rebuilt at all. This reconstructs them from
the mirrored Overture and Foursquare releases.

    build_poi_extracts.py <out_dir> [--states TN,RI]

Writes {ST}.parquet per state, plus chain_index.parquet and brand_map.parquet
in the same directory. Point build_full_ptilesb.py at it with STATE_DIR.

State assignment comes from each source's own region field, not from a bounding
box: Foursquare carries `region` and Overture carries `addresses.region`, both
already state-level, so no point-in-polygon step is needed or wanted.

Overture and Foursquare are unioned without cross-source deduplication, which is
what the archived merge_us_poi.py did -- a place present in both appears twice,
under different source ids.
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from states import STATES, get_state

SNAPSHOT = Path("/mnt/core/timeline-ptiles-cache/2026-08-06")
OVERTURE = SNAPSHOT / "overture/theme=places/type=place/*.zstd.parquet"
FOURSQUARE = SNAPSHOT / "foursquare/release/dt=2026-07-09/places/parquet/places_*.parquet"

# Foursquare publishes no confidence score, so those rows carry NULL rather than
# an invented one; build_full_ptilesb maps NULL to 0 and drops the field.
UNIFIED_SQL = """
CREATE OR REPLACE VIEW poi AS
SELECT
    'overture'                       AS source,
    id                               AS source_id,
    names.primary                    AS name,
    ST_Y(geometry)                   AS lat,
    ST_X(geometry)                   AS lon,
    categories.primary               AS primary_category,
    addresses[1].freeform            AS address,
    addresses[1].locality            AS city,
    upper(addresses[1].region)       AS state_abbr,
    phones[1]                        AS phone,
    websites[1]                      AS website,
    confidence                       AS confidence,
    brand.names.primary              AS brand_name,
    -- names.common is a language->value map; 'en' is the alternative name worth
    -- carrying. It was dropped at this projection, not by upstream.
    names.common['en']               AS name_en
FROM read_parquet('{overture}')
WHERE addresses[1].country = 'US'
  AND names.primary IS NOT NULL
  AND (operating_status IS NULL OR operating_status = 'open')

UNION ALL BY NAME

SELECT
    'foursquare'                     AS source,
    fsq_place_id                     AS source_id,
    name                             AS name,
    latitude                         AS lat,
    longitude                        AS lon,
    fsq_category_labels[1]           AS primary_category,
    address                          AS address,
    locality                         AS city,
    upper(region)                    AS state_abbr,
    tel                              AS phone,
    website                          AS website,
    CAST(NULL AS DOUBLE)             AS confidence,
    CAST(NULL AS VARCHAR)            AS brand_name,
    CAST(NULL AS VARCHAR)            AS name_en
FROM read_parquet('{foursquare}')
WHERE country = 'US'
  AND date_closed IS NULL
  AND name IS NOT NULL
  AND latitude IS NOT NULL AND longitude IS NOT NULL
"""

EXTRACT_COLUMNS = (
    "source, source_id, name, lat, lon, primary_category, "
    "address, city, phone, website, confidence, brand_name, name_en"
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("out_dir")
    p.add_argument("--states", help="Comma-separated abbreviations (default: all 51)")
    args = p.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if args.states:
        targets = [s for s in (get_state(a.strip()) for a in args.states.split(",")) if s]
    else:
        targets = list(STATES)

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(UNIFIED_SQL.format(overture=OVERTURE, foursquare=FOURSQUARE))

    t0 = time.time()
    print(f"[extract] {len(targets)} states from {SNAPSHOT}", flush=True)

    # One partitioned pass, not one COPY per state: a per-state WHERE rescans all
    # 21 GB every time, which measured at 93s -- 51 of those is over an hour of
    # re-reading the same bytes.
    # Both sources carry a small share of records whose region says one state and
    # whose coordinates are in another -- 0.1%, but enough to wreck the header
    # bbox the client routes on: Rhode Island measured out to lon -123.4, which is
    # California. Requiring the point to fall in the state's own (Census-derived)
    # box drops those without touching the 99.9% that agree.
    boxes = ",".join(
        f"('{s.abbr}',{s.min_lat},{s.min_lon},{s.max_lat},{s.max_lon})" for s in targets
    )
    staging = out / "_partitioned"
    con.execute(
        f"COPY (SELECT {EXTRACT_COLUMNS}, poi.state_abbr FROM poi "
        f"JOIN (VALUES {boxes}) AS b(abbr, min_lat, min_lon, max_lat, max_lon) "
        f"  ON poi.state_abbr = b.abbr "
        f"WHERE poi.lat BETWEEN b.min_lat AND b.max_lat "
        f"  AND poi.lon BETWEEN b.min_lon AND b.max_lon) TO '{staging}' "
        f"(FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (state_abbr), OVERWRITE_OR_IGNORE)"
    )
    for s in targets:
        part = staging / f"state_abbr={s.abbr}"
        files = sorted(part.glob("*.parquet")) if part.is_dir() else []
        dest = out / f"{s.abbr}.parquet"
        if not files:
            print(f"  {s.abbr:2s} no rows -- skipped", flush=True)
            continue
        # PARTITION_BY drops the partition column from the file, but the builder
        # never reads state_abbr back, so a rename is enough for the usual
        # one-file partition. Concatenate rather than assume, since picking the
        # first of several would drop rows without saying so.
        if len(files) == 1:
            files[0].replace(dest)
        else:
            con.execute(
                f"COPY (SELECT {EXTRACT_COLUMNS} FROM "
                f"read_parquet({[str(f) for f in files]!r})) "
                f"TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            for f in files:
                f.unlink()
        n = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
        print(f"  {s.abbr:2s} {n:9,d} places  {dest.stat().st_size:11,d} B", flush=True)

    # Chain index: how many times a name repeats nationally, which is how the
    # builder decides a place is part of a chain.
    con.execute(
        f"COPY (SELECT lower(name) AS name, count(*) AS count FROM poi "
        f"WHERE name IS NOT NULL GROUP BY 1 HAVING count(*) > 1) "
        f"TO '{out / 'chain_index.parquet'}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )

    # Brand map: Overture only -- Foursquare publishes no brand field.
    con.execute(
        f"COPY (SELECT source_id AS overture_id, brand_name FROM poi "
        f"WHERE source = 'overture' AND brand_name IS NOT NULL) "
        f"TO '{out / 'brand_map.parquet'}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    for f in ("chain_index.parquet", "brand_map.parquet"):
        n = con.execute(f"SELECT count(*) FROM read_parquet('{out / f}')").fetchone()[0]
        print(f"  {f:22} {n:9,d} rows", flush=True)

    shutil.rmtree(staging, ignore_errors=True)
    print(f"POI_EXTRACTS_COMPLETE {len(targets)} states in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
