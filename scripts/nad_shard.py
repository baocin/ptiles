#!/usr/bin/env python3
"""Split the NAD release into per-state CSV shards, streaming from the zip.

The release is one 41 GB text member holding 97.9M rows across 48 states. A
per-state builder that filtered it itself would read those 41 GB once per
state; sharding reads them once, total, and leaves each state a file it can
load in seconds.

Only the five fields the address layer stores survive the split: number,
street, unit, lon, lat. `Unit` is on 22% of NAD's rows and is what distinguishes
one apartment from the next at a single point -- without it the merge folds a
whole building onto one record. Everything else in NAD's 59-column schema (parcel ids,
lifecycle dates, census places) is dropped here rather than carried through
the build and discarded at encode time.

Output: {DEST}/{ST}.csv  --  number,street,unit,lon,lat
"""
import csv
import io
import sys
import zipfile
from pathlib import Path

ZIP = Path("/mnt/core/timeline-ptiles-cache/addresses/nad/NAD_r23_TXT.zip")
MEMBER = "TXT/NAD_r23.txt"
DEST = Path("/mnt/core/timeline-ptiles-cache/addresses/nad/states")

# NAD splits the house number across Add_Number plus optional prefix/suffix,
# and also ships AddNo_Full with all three already joined. Prefer the joined
# form and fall back, because a handful of rows carry only the parts.
NUMBER_FULL, NUMBER_PARTS = "AddNo_Full", ("AddNum_Pre", "Add_Number", "AddNum_Suf")
# StNam_Full is likewise the pre-joined "N MAIN ST"; St_Name alone is "MAIN".
STREET_FULL = "StNam_Full"


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    z = zipfile.ZipFile(ZIP)
    with z.open(MEMBER) as fh:
        text = io.TextIOWrapper(fh, "utf-8", errors="ignore", newline="")
        r = csv.reader(text)
        header = next(r)
        # The first column name carries a UTF-8 BOM; strip it so a lookup by
        # name works for every column including the first.
        idx = {n.lstrip("﻿").strip().lower(): i for i, n in enumerate(header)}
        need = ["state", "longitude", "latitude", "unit", NUMBER_FULL.lower(), STREET_FULL.lower()]
        missing = [n for n in need if n not in idx]
        if missing:
            sys.exit(f"NAD schema changed, missing {missing}: {header}")
        i_st, i_lon, i_lat = idx["state"], idx["longitude"], idx["latitude"]
        i_num, i_street = idx[NUMBER_FULL.lower()], idx[STREET_FULL.lower()]
        i_unit = idx["unit"]
        i_parts = [idx[p.lower()] for p in NUMBER_PARTS if p.lower() in idx]

        writers, handles = {}, {}
        kept = dropped = 0
        for n, row in enumerate(r, 1):
            if len(row) <= max(i_st, i_lon, i_lat, i_num, i_street):
                dropped += 1
                continue
            st = row[i_st].strip().upper()
            num = row[i_num].strip() or "".join(row[p].strip() for p in i_parts)
            street = row[i_street].strip()
            lon, lat = row[i_lon].strip(), row[i_lat].strip()
            # A record with no number, no street or no position cannot answer
            # either direction of geocoding; it is not worth a row.
            if not (st and num and street and lon and lat):
                dropped += 1
                continue
            w = writers.get(st)
            if w is None:
                handles[st] = open(DEST / f"{st}.csv", "w", newline="")
                w = writers[st] = csv.writer(handles[st])
                w.writerow(["number", "street", "unit", "lon", "lat"])
            unit = row[i_unit].strip() if len(row) > i_unit else ""
            w.writerow([num, street, unit, lon, lat])
            kept += 1
            if n % 10_000_000 == 0:
                print(f"  {n:,} rows read, {kept:,} kept", flush=True)

        for h in handles.values():
            h.close()

    print(f"total read, {kept:,} kept, {dropped:,} dropped, {len(writers)} states")
    for p in sorted(DEST.glob("*.csv")):
        print(f"  {p.name:8s} {p.stat().st_size:>12,} B")


if __name__ == "__main__":
    main()
