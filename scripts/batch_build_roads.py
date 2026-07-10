#!/usr/bin/env python3
"""
Build all 51 state roads files using the new PTLR format.
Reads PBFs from /mnt/core/timeline-ptiles-cache/raw/,
writes to /mnt/core/timeline-ptiles-cache/roads/ (or a specified output dir).

Usage:
  python3 batch_build_roads.py [output_dir]
"""

import os
import sys
import subprocess
import time

PBF_DIR = "/mnt/core/timeline-ptiles-cache/raw"


def main():
    output_dir = (
        sys.argv[1] if len(sys.argv) > 1 else "/mnt/core/timeline-ptiles-cache/roads"
    )
    os.makedirs(output_dir, exist_ok=True)

    # Get all PBFs, sorted
    pbfs = sorted(
        [
            f
            for f in os.listdir(PBF_DIR)
            if f.endswith(".osm.pbf") and not f.startswith("._")
        ]
    )
    print(f"Found {len(pbfs)} PBFs in {PBF_DIR}")

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_roads.py")
    uv_cmd = [
        "uv",
        "run",
        "--with",
        "osmium",
        "--with",
        "h3",
        "--with",
        "zstandard",
        "--with",
        "shapely",
    ]

    for i, pbf_name in enumerate(pbfs):
        state_name = pbf_name.replace("-latest.osm.pbf", "").replace(".osm.pbf", "")
        # Map name to state code
        STATE_CODES = {
            "alabama": "AL",
            "alaska": "AK",
            "arizona": "AZ",
            "arkansas": "AR",
            "california": "CA",
            "colorado": "CO",
            "connecticut": "CT",
            "delaware": "DE",
            "district-of-columbia": "DC",
            "florida": "FL",
            "georgia": "GA",
            "hawaii": "HI",
            "idaho": "ID",
            "illinois": "IL",
            "indiana": "IN",
            "iowa": "IA",
            "kansas": "KS",
            "kentucky": "KY",
            "louisiana": "LA",
            "maine": "ME",
            "maryland": "MD",
            "massachusetts": "MA",
            "michigan": "MI",
            "minnesota": "MN",
            "mississippi": "MS",
            "missouri": "MO",
            "montana": "MT",
            "nebraska": "NE",
            "nevada": "NV",
            "new-hampshire": "NH",
            "new-jersey": "NJ",
            "new-mexico": "NM",
            "new-york": "NY",
            "north-carolina": "NC",
            "north-dakota": "ND",
            "ohio": "OH",
            "oklahoma": "OK",
            "oregon": "OR",
            "pennsylvania": "PA",
            "rhode-island": "RI",
            "south-carolina": "SC",
            "south-dakota": "SD",
            "tennessee": "TN",
            "texas": "TX",
            "utah": "UT",
            "vermont": "VT",
            "virginia": "VA",
            "washington": "WA",
            "west-virginia": "WV",
            "wisconsin": "WI",
            "wyoming": "WY",
        }
        state_code = STATE_CODES.get(state_name, state_name[:2].upper())
        output_path = os.path.join(output_dir, f"{state_code}.roads.ptiles")

        # Skip if already exists
        if os.path.exists(output_path):
            print(f"[{i + 1}/{len(pbfs)}] {state_code} already exists, skipping")
            continue

        pbf_path = os.path.join(PBF_DIR, pbf_name)
        print(f"[{i + 1}/{len(pbfs)}] {state_code}: building from {pbf_name}...")
        t0 = time.time()

        result = subprocess.run(
            uv_cmd + ["python", script, pbf_path, output_path],
            capture_output=True,
            text=True,
            timeout=3600,
        )

        elapsed = time.time() - t0
        if result.returncode == 0:
            # Get final size
            size_mb = (
                os.path.getsize(output_path) / 1_048_576
                if os.path.exists(output_path)
                else 0
            )
            print(f"  {state_code}: DONE in {elapsed:.0f}s, {size_mb:.1f} MB")
            # Print last 3 lines of output for summary
            for line in result.stdout.strip().split("\n")[-3:]:
                print(f"  {line}")
        else:
            print(f"  {state_code}: FAILED (exit {result.returncode})")
            for line in result.stderr.strip().split("\n")[-5:]:
                print(f"  {line}")

        # Flush
        sys.stdout.flush()


if __name__ == "__main__":
    main()
