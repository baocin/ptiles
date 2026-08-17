#!/usr/bin/env python3
"""Count NAD r23 records per state, streaming the 41 GB text member out of the
zip. Nothing is extracted to disk: at 41 GB the extract would cost more than
the scan, and every consumer of this data wants it filtered anyway."""
import csv, io, sys, zipfile
from collections import Counter

ZIP = "/mnt/core/timeline-ptiles-cache/addresses/nad/NAD_r23_TXT.zip"
MEMBER = "TXT/NAD_r23.txt"

z = zipfile.ZipFile(ZIP)
with z.open(MEMBER) as fh:
    text = io.TextIOWrapper(fh, "utf-8", errors="ignore", newline="")
    r = csv.reader(text)
    header = next(r)
    print("fields:", ",".join(header), flush=True)
    # The state column is named State in the current schema; fall back to a
    # scan so a schema rename fails loudly rather than counting nothing.
    idx = {n.lower(): i for i, n in enumerate(header)}
    si = idx.get("state")
    if si is None:
        sys.exit(f"no State column in: {header}")
    c = Counter()
    for i, row in enumerate(r, 1):
        if len(row) > si:
            c[row[si]] += 1
        if i % 5_000_000 == 0:
            print(f"  {i:,} rows", flush=True)

print(f"total {sum(c.values()):,} rows, {len(c)} states")
for st, n in sorted(c.items(), key=lambda kv: -kv[1]):
    print(f"  {st:4s} {n:>10,}")
