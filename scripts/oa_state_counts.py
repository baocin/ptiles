#!/usr/bin/env python3
"""Row counts per state across the four OpenAddresses collection zips.

Counts rows, not addresses: OA ships overlapping sources for the same ground
(a statewide file plus county and city files that re-cover its cities), so a
state's real address count is somewhere below its row count. The point here is
which states OA covers at all -- NAD is empty or near-empty in several.
"""
import io, zipfile
from collections import Counter
from pathlib import Path

DEST = Path("/mnt/core/timeline-ptiles-cache/addresses/openaddresses")
rows, files = Counter(), Counter()
for zp in sorted(DEST.glob("*.zip")):
    z = zipfile.ZipFile(zp)
    for i in z.infolist():
        parts = i.filename.split("/")
        if len(parts) < 3 or parts[0] != "us" or not i.filename.endswith(".csv"):
            continue
        st = parts[1].upper()
        n = 0
        with z.open(i) as f:
            for _ in io.TextIOWrapper(f, "utf-8", errors="ignore"):
                n += 1
        rows[st] += max(0, n - 1)
        files[st] += 1
    print(f"  done {zp.name}", flush=True)

print(f"total {sum(rows.values()):,} rows, {len(rows)} states")
for st, n in sorted(rows.items(), key=lambda kv: -kv[1]):
    print(f"  {st:3s} {n:>10,}  ({files[st]} files)")
