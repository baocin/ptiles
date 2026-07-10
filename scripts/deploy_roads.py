#!/usr/bin/env python3
"""
Upload all PTLR-format roads files to S3 and invalidate CloudFront.

Usage:
  python3 deploy_roads.py [source_dir]

Default source_dir: ~/kino/projects/ptiles/data/states/roads/
"""

import os
import subprocess
import sys

S3_BUCKET = "mydatatimeline"
S3_PREFIX = "maps"
CF_DISTRIBUTION_ID = "E1F8I1L0K0J0V0"  # Update with actual CF ID
AWS_PROFILE = "mdt-r2"


def main():
    source_dir = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.expanduser("~/kino/projects/ptiles/data/states/roads")
    )

    # Find all .roads.ptiles files
    files = sorted(
        [
            f
            for f in os.listdir(source_dir)
            if f.endswith(".roads.ptiles") and len(f) == 15
        ]
    )  # XX.roads.ptiles = 15 chars
    print(f"Found {len(files)} road files to upload")

    if not files:
        files = sorted(
            [f for f in os.listdir(source_dir) if f.endswith(".roads.ptiles")]
        )
        print(f"Found {len(files)} road files (non-standard names)")

    uploaded = []
    for fname in files:
        local_path = os.path.join(source_dir, fname)
        s3_path = f"s3://{S3_BUCKET}/{S3_PREFIX}/{fname}"

        result = subprocess.run(
            ["aws", "s3", "cp", local_path, s3_path, "--profile", AWS_PROFILE],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"  {fname}: uploaded")
            uploaded.append(fname)
        else:
            print(f"  {fname}: FAILED - {result.stderr.strip()}")

    # Invalidate CloudFront
    if uploaded:
        paths = [f"/{S3_PREFIX}/{f}" for f in uploaded]
        print(f"\nInvalidating {len(paths)} paths...")
        result = subprocess.run(
            [
                "aws",
                "cloudfront",
                "create-invalidation",
                "--distribution-id",
                CF_DISTRIBUTION_ID,
                "--paths",
                *paths,
                "--profile",
                AWS_PROFILE,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("  Invalidation created")
        else:
            print(f"  Invalidation failed: {result.stderr.strip()}")

    print(f"\nDone. {len(uploaded)}/{len(files)} files uploaded.")


if __name__ == "__main__":
    main()
