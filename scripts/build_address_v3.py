#!/usr/bin/env python3
"""Build {STATE}.address_v3.ptiles: OSM + NAD + OpenAddresses, merged.

Why three sources. Each one alone leaves states unanswerable, and they fail in
different places -- measured 2026-08-09:

    Tennessee     OSM   137,110    NAD 3,892,997    OA  4,154,511 rows
    Florida       OSM 1,967,044    NAD    42,466    OA 38,862,561 rows
    Mississippi   OSM    77,000    NAD         3    OA  1,133,106 rows

so "one source, others as fallback" has no defensible ordering. All three are
merged per cell and each record keeps a byte saying where it came from.

v3 = v2 plus that byte, written after the i16 coordinate offsets so everything
a v2 reader knows how to find stays where it expects it. Blocks hold ONE cell
rather than eight: measured on the v2 files, an 8-cell block reaches 494 KB in
Manhattan, and a click that wants one cell pays for all eight. The dictionary
is trained across states, which is what buys back the compression context that
smaller blocks give up.

Inputs:
  OSM   {PBF_DIR}/{state}.osm.pbf                       (see build_address.py)
  NAD   {NAD_DIR}/{ST}.csv                              (see nad_shard.py)
  OA    {OA_DIR}/openaddr-collected-us_{region}.zip     us/{st}/*.csv
"""
import csv
import io
import os
import re
import struct
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

import h3
import osmium
import zstandard as zstd

from shared import (
    choose_dictionary,
    encode_varint,
    zigzag_encode,
    encode_merged_block,
    write_header,
    HEADER_SIZE,
    encode_index_entry_v2,
    INDEX_ENTRY_SIZE_V2,
)
from states import STATES, get_state
from build_address import PBF_MAP, PBF_DIR

OUTPUT_DIR = Path("/mnt/core/kino/ptiles/data/v4/states")
OUT_SUFFIX = "address_v4"
NAD_DIR = Path("/mnt/core/timeline-ptiles-cache/addresses/nad/states")
OA_DIR = Path("/mnt/core/timeline-ptiles-cache/addresses/openaddresses")
DICT_PATH = Path("/mnt/core/timeline-ptiles-cache/addresses/address_v3.dict")

MAGIC = b"PTILESD\x00"
VERSION = 4
H3_RES = 7

SRC_OSM, SRC_NAD, SRC_OA = 0, 1, 2
# Which source wins when two of them describe the same address. NAD is
# authoritative municipal data; OA aggregates the same kind of source but with
# more duplication and looser normalisation; OSM is hand-mapped and, in the
# states where it is strong, the only source at all -- so it stays last for
# collisions while still contributing every address the other two lack.
SRC_PRIORITY = {SRC_NAD: 0, SRC_OA: 1, SRC_OSM: 2}

OA_REGIONS = {
    "us_northeast": "CT DE MA MD ME NH NJ NY PA RI VT".split(),
    "us_midwest": "IA IL IN KS MI MN MO ND NE OH SD WI".split(),
    "us_south": "AL AR DC FL GA KY LA MS NC OK SC TN TX VA WV".split(),
    "us_west": "AK AZ CA CO HI ID MT NM NV OR UT WA WY".split(),
}

# --- normalisation -------------------------------------------------------
#
# The same house reaches us as "123 N Main St" (NAD), "123 NORTH MAIN STREET"
# (OA) and "123 North Main Street" (OSM). Dedupe compares a folded form; the
# record still stores the winning source's own spelling, because that is what
# a user reads.
_SUFFIX = {
    "ST": "ST", "STREET": "ST", "AVE": "AVE", "AV": "AVE", "AVENUE": "AVE",
    "RD": "RD", "ROAD": "RD", "DR": "DR", "DRIVE": "DR", "LN": "LN",
    "LANE": "LN", "CT": "CT", "COURT": "CT", "BLVD": "BLVD",
    "BOULEVARD": "BLVD", "HWY": "HWY", "HIGHWAY": "HWY", "PKWY": "PKWY",
    "PARKWAY": "PKWY", "CIR": "CIR", "CIRCLE": "CIR", "PL": "PL",
    "PLACE": "PL", "TER": "TER", "TERRACE": "TER", "TRL": "TRL",
    "TRAIL": "TRL", "WAY": "WAY", "PIKE": "PIKE", "LOOP": "LOOP",
}
_DIR = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_SPACE = re.compile(r"\s+")


def fold_street(s):
    s = _PUNCT.sub(" ", s.upper())
    out = []
    for w in _SPACE.sub(" ", s).strip().split(" "):
        out.append(_DIR.get(w, _SUFFIX.get(w, w)))
    return " ".join(out)


def fold_number(s):
    return _PUNCT.sub("", s.upper())


_ORDINAL = re.compile(r"^(\d+)(ST|ND|RD|TH)$")
_KEEP_UPPER = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "US", "SR", "I", "PO"}


def prettify(s):
    """Case a shouted string for display. NAD and OpenAddresses ship
    `10TH AVE N`; OSM ships `10th Avenue North`. Since NAD wins precedence,
    leaving it alone would turn a working demo's panel into all caps.

    A string that already has lower-case letters is left exactly as it is --
    that is a human-entered name and any transformation can only damage it.
    """
    if any(c.islower() for c in s):
        return s
    out = []
    for w in s.split():
        m = _ORDINAL.match(w)
        if m:
            out.append(m.group(1) + m.group(2).lower())
        elif w in _KEEP_UPPER:
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)


def usable(number, street):
    """A parcel with house number 0 is a lot, not an address -- 23,865 of
    Tennessee's OpenAddresses rows are these, and they answer no question a
    user can ask."""
    fn = fold_number(number)
    return bool(fn) and bool(street.strip()) and fn.strip("0") != ""


# ~111 m of latitude, checked across the 3x3 of grid squares around a record.
#
# Both halves were measured on Tennessee (7,628,531 raw rows in):
#
#     11 m grid,  exact square      5,706,495 kept
#     11 m grid,  3x3 neighbours    4,706,110
#     111 m grid, exact square      4,434,403
#     111 m grid, 3x3 neighbours    4,030,400
#
# against ~3.0M housing units, so the tight-and-exact variant was leaving 1.7M
# duplicates in. Two reasons. A grid square only matches records that land in
# the same square, so two points 2 m apart across a boundary never meet --
# worth a million rows by itself. And NAD and OpenAddresses disagree about
# where a house *is* by more than 11 m routinely, one placing the structure
# and the other the parcel centroid.
#
# The remaining risk is the same house number appearing twice on one street
# within ~330 m, which needs two towns to share a street name and abut that
# closely; that is rarer than the duplicates this removes.
GRID = 1000


def grid_cell(lat, lon):
    return round(lat * GRID), round(lon * GRID)


def fold_unit(s):
    """`APT B`, `Apt. B` and `#B` are the same unit; `APT B` and `APT C` are
    not. Without this the merge folds a whole apartment building onto one
    record -- 22% of NAD's rows and 5% of OpenAddresses' carry a unit."""
    return _PUNCT.sub(" ", s.upper()).replace("APARTMENT", "APT").split()


def key(number, street, lat, lon, unit=""):
    gy, gx = grid_cell(lat, lon)
    return (fold_number(number), fold_street(street), tuple(fold_unit(unit)), gy, gx)


# --- sources -------------------------------------------------------------


class AddrExtractor(osmium.SimpleHandler):
    """Every addr:housenumber in the extract, node or way. Same rule as
    build_address.py -- see its docstring for why `building` is not required."""

    def __init__(self):
        super().__init__()
        self.addrs = []

    def _add(self, osm_id, lat, lon, hn, st, unit):
        self.addrs.append((hn, st, lat, lon, SRC_OSM, osm_id, unit))

    def node(self, n):
        hn = n.tags.get("addr:housenumber")
        if hn and n.location.valid():
            self._add(
                n.id, n.location.lat, n.location.lon, hn,
                n.tags.get("addr:street") or "", n.tags.get("addr:unit") or "",
            )

    def way(self, w):
        hn = w.tags.get("addr:housenumber")
        if not hn or not w.nodes:
            return
        for nd in w.nodes:
            if nd.location.valid():
                self._add(
                    w.id, nd.lat, nd.lon, hn,
                    w.tags.get("addr:street") or "", w.tags.get("addr:unit") or "",
                )
                return


def read_osm(abbr):
    pbfn = PBF_MAP.get(abbr)
    if not pbfn:
        return []
    p = PBF_DIR / f"{pbfn}.osm.pbf"
    if not p.exists():
        return []
    h = AddrExtractor()
    h.apply_file(str(p), locations=True)
    return h.addrs


def read_nad(abbr):
    """Yields rather than returns: New York's three sources are 20M rows, and
    materialising a source list *beside* the dedupe dict doubles peak memory
    for no reason."""
    p = NAD_DIR / f"{abbr}.csv"
    if not p.exists():
        return
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            try:
                yield (row["number"], row["street"], float(row["lat"]), float(row["lon"]),
                       SRC_NAD, 0, (row.get("unit") or "").strip())
            except (ValueError, TypeError, KeyError):
                continue


def read_oa(abbr):
    region = next((r for r, sts in OA_REGIONS.items() if abbr in sts), None)
    if not region:
        return
    zp = OA_DIR / f"openaddr-collected-{region}.zip"
    if not zp.exists():
        return
    z = zipfile.ZipFile(zp)
    prefix = f"us/{abbr.lower()}/"
    for info in z.infolist():
        if not info.filename.startswith(prefix) or not info.filename.endswith(".csv"):
            continue
        with z.open(info) as fh:
            for row in csv.DictReader(io.TextIOWrapper(fh, "utf-8", errors="ignore")):
                num, street = (row.get("NUMBER") or "").strip(), (row.get("STREET") or "").strip()
                if not num or not street:
                    continue
                try:
                    yield (num, street, float(row["LAT"]), float(row["LON"]), SRC_OA, 0,
                           (row.get("UNIT") or "").strip())
                except (ValueError, TypeError, KeyError):
                    continue


# Degrees of slack on the state bbox: border addresses legitimately sit just
# outside a coarse state box, and this only exists to drop ocean-and-Canada
# geocoding failures, not to enforce a boundary.
BBOX_MARGIN_DEG = 0.25


def merge(abbr):
    """All three sources, deduped. Returns [(number, street, lat, lon, source, osm_id)].

    A record matches an existing one if they agree on folded number and street
    and sit in the same or an adjoining grid square -- see GRID for why the
    neighbourhood check is not optional.
    """
    best = {}
    counts = defaultdict(int)
    st = get_state(abbr)
    bounds = (st.min_lat, st.min_lon, st.max_lat, st.max_lon) if st else None
    for reader in (read_nad, read_oa, read_osm):
        for rec in reader(abbr):
            number, street, lat, lon, source, _, unit = rec
            counts[source] += 1
            if not usable(number, street):
                counts["dropped"] += 1
                continue
            # Outside the state's own bounds, plus a margin for border towns
            # and imprecise state boxes. California's OpenAddresses set puts
            # 636 addresses in the Pacific -- "135 Sam McDonald Road" at
            # 30.01N 141.22W -- which are geocoding failures upstream, not
            # places anyone can click.
            if bounds and not (
                bounds[0] - BBOX_MARGIN_DEG <= lat <= bounds[2] + BBOX_MARGIN_DEG
                and bounds[1] - BBOX_MARGIN_DEG <= lon <= bounds[3] + BBOX_MARGIN_DEG
            ):
                counts["out_of_state"] += 1
                continue
            rec = (prettify(number), prettify(street), lat, lon, source, rec[5],
                   prettify(unit))
            fn, fs = fold_number(number), fold_street(street)
            fu = tuple(fold_unit(unit))
            gy, gx = grid_cell(lat, lon)
            found = None
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if (fn, fs, fu, gy + dy, gx + dx) in best:
                        found = (fn, fs, fu, gy + dy, gx + dx)
                        break
                if found:
                    break
            if found is None:
                best[(fn, fs, fu, gy, gx)] = rec
            elif SRC_PRIORITY[source] < SRC_PRIORITY[best[found][4]]:
                # Better source for an address already held: keep the slot it
                # was found under so a third copy still matches, take its data.
                best[found] = rec
    return list(best.values()), counts


# --- encoding ------------------------------------------------------------


def enc(rec, pid, centre_micro):
    """One v3 record: delta id, i16 lon/lat offsets, source byte, strings."""
    number, street, lat, lon, source, osm_id, unit = rec
    b = bytearray()
    b.extend(encode_varint(zigzag_encode(osm_id - pid)))
    b.extend(
        struct.pack(
            "<hh",
            round(lon * 100000) - centre_micro[0],
            round(lat * 100000) - centre_micro[1],
        )
    )
    b.append(source)
    for s in (number, street, unit):
        raw = s.encode("utf-8")[:65535]
        b.extend(struct.pack("<H", len(raw)))
        b.extend(raw)
    return bytes(b)


# The i16 offsets span +-0.32768 degrees from a cell centre. A record inside
# its own res-7 cell (~0.02 degrees) cannot approach that -- except at a pole,
# where the cell's centre longitude is arbitrary:
#
#     lat 90.0, lon 0.0  ->  centre 89.99507, 174.60352  ->  lon offset -17,460,352
#
# and near the antimeridian, where a point and its cell centre can sit on
# opposite sides of it. Both are junk coordinates rather than real addresses,
# but one of them anywhere in a state used to abort that state's entire build
# with a struct.error.
OFFSET_LIMIT = 32767


def blocks_for(records):
    """Group into one block per cell.

    Returns (sorted_cells, {cell: (record_bytes, records)}, dropped)."""
    per_cell = defaultdict(list)
    dropped = 0
    for rec in records:
        lat, lon = rec[2], rec[3]
        if not (-85.0 <= lat <= 85.0) or not (-180.0 <= lon <= 180.0):
            dropped += 1
            continue
        try:
            cell = h3.latlng_to_cell(lat, lon, H3_RES)
        except Exception:
            dropped += 1
            continue
        per_cell[int(cell, 16) if isinstance(cell, str) else cell].append(rec)

    encoded = {}
    for cell, recs in per_cell.items():
        # Sort by osm_id so the delta chain is monotone for OSM records; the
        # bulk sources carry no id, so their deltas are all zero and cost one
        # byte each rather than a varint of a random 10-digit number.
        recs.sort(key=lambda r: (r[5], r[0], r[1]))
        clat, clon = h3.cell_to_latlng(hex(cell)[2:])
        centre = (round(clon * 100000), round(clat * 100000))
        out, kept, pid = [], [], 0
        for r in recs:
            # Belt to the range check above's braces: an offset that will not
            # fit is dropped here rather than raising and losing the state.
            if (
                abs(round(r[3] * 100000) - centre[0]) > OFFSET_LIMIT
                or abs(round(r[2] * 100000) - centre[1]) > OFFSET_LIMIT
            ):
                dropped += 1
                continue
            out.append(enc(r, pid, centre))
            kept.append(r)
            pid = r[5]
        if out:
            encoded[cell] = (out, kept)
    return sorted(encoded.keys()), encoded, dropped


def build(abbr, compressor, dict_bytes):
    t0 = time.time()
    records, counts = merge(abbr)
    if not records:
        return {"abbr": abbr, "addrs": 0, "time_s": round(time.time() - t0, 1)}

    sorted_cells, encoded, dropped = blocks_for(records)
    raw_blocks, index_rows = [], []
    for cell in sorted_cells:
        recs_bytes, recs = encoded[cell]
        clat, clon = h3.cell_to_latlng(hex(cell)[2:])
        raw_blocks.append(
            encode_merged_block(
                [(cell, recs_bytes)], round(clon * 100000), round(clat * 100000)
            )
        )
        index_rows.append(
            {
                "h3_cell": cell,
                # feature_count is a u16; a cell with more addresses than that
                # still encodes every record (the reader walks the slice to its
                # end), so saturate rather than truncate the data.
                "feature_count": min(len(recs), 0xFFFF),
                "cell_index": 0,
                "min_lon": round(min(r[3] for r in recs) * 100000),
                "min_lat": round(min(r[2] for r in recs) * 100000),
                "max_lon": round(max(r[3] for r in recs) * 100000),
                "max_lat": round(max(r[2] for r in recs) * 100000),
            }
        )

    compressed = [compressor.compress(b) for b in raw_blocks]

    dict_offset = HEADER_SIZE
    dict_length = len(dict_bytes)
    index_offset = dict_offset + dict_length
    index_length = 4 + len(index_rows) * INDEX_ENTRY_SIZE_V2
    blocks_offset = index_offset + index_length

    index_bytes = bytearray(struct.pack("<I", len(index_rows)))
    off = blocks_offset
    for row, blob in zip(index_rows, compressed):
        index_bytes.extend(
            encode_index_entry_v2(block_offset=off, block_length=len(blob), **row)
        )
        off += len(blob)

    s = get_state(abbr)
    op = OUTPUT_DIR / f"{abbr}.{OUT_SUFFIX}.ptiles"
    with open(op, "wb") as f:
        write_header(
            f, MAGIC, VERSION,
            s.min_lat, s.min_lon, s.max_lat, s.max_lon,
            sum(len(encoded[c][1]) for c in sorted_cells), len(compressed),
            dict_offset, dict_length, index_offset, index_length, blocks_offset,
        )
        f.write(dict_bytes)
        f.write(bytes(index_bytes))
        for blob in compressed:
            f.write(blob)

    return {
        "abbr": abbr,
        "addrs": len(records),
        "raw": sum(counts.values()),
        "cells": len(sorted_cells),
        "bytes": op.stat().st_size,
        "dropped": dropped,
        "out_of_state": counts["out_of_state"],
        "units": sum(1 for r in records if r[6]),
        "osm": counts[SRC_OSM],
        "nad": counts[SRC_NAD],
        "oa": counts[SRC_OA],
        "time_s": round(time.time() - t0, 1),
    }


def train_dictionary(sample_states, size=110_000):
    """Train one dictionary across several states and reuse it for all of them.

    Per-state dictionaries were fine when a block held eight cells and its own
    redundancy; one cell per block has far less internal context, and street
    names repeat across state lines anyway.
    """
    if DICT_PATH.exists():
        print(f"  using cached dictionary {DICT_PATH} ({DICT_PATH.stat().st_size:,} B)")
        return DICT_PATH.read_bytes()
    samples = []
    for abbr in sample_states:
        records, _ = merge(abbr)
        if not records:
            continue
        cells, encoded, _ = blocks_for(records)
        for cell in cells[:4000]:
            recs_bytes, _ = encoded[cell]
            clat, clon = h3.cell_to_latlng(hex(cell)[2:])
            samples.append(
                encode_merged_block(
                    [(cell, recs_bytes)], round(clon * 100000), round(clat * 100000)
                )
            )
        print(f"  dict sample {abbr}: {len(samples):,} blocks so far")
    d = zstd.train_dictionary(size, samples).as_bytes()
    DICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DICT_PATH.write_bytes(d)
    print(f"  trained dictionary {len(d):,} B from {len(samples):,} blocks")
    return d


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true")
    p.add_argument("--states")
    p.add_argument(
        "--dict-states",
        default="TN,FL,NY,MT",
        help="states sampled to train the shared dictionary: one NAD-strong, "
        "one OA-only, one dense-urban, one rural",
    )
    args = p.parse_args()

    if args.all:
        targets = [s.abbr for s in STATES]
    elif args.states:
        targets = [get_state(a.strip()).abbr for a in args.states.split(",") if get_state(a.strip())]
    else:
        p.print_help()
        return

    dict_bytes = train_dictionary([a.strip() for a in args.dict_states.split(",")])
    zd = zstd.ZstdCompressionDict(dict_bytes)
    compressor = zstd.ZstdCompressor(level=12, dict_data=zd)

    for abbr in targets:
        try:
            r = build(abbr, compressor, dict_bytes)
            if r.get("addrs"):
                print(
                    f"  {r['abbr']:2s} {r['addrs']:9,d} addrs "
                    f"(osm {r['osm']:>9,d} nad {r['nad']:>9,d} oa {r['oa']:>10,d} "
                    f"raw {r['raw']:>10,d}) {r['cells']:>6,d} cells "
                    f"{r['bytes']:>12,d} B {r['time_s']:7.1f}s"
                    + (f"  units {r['units']:,}" if r.get("units") else "")
                    + (f"  out-of-state {r['out_of_state']:,}" if r.get("out_of_state") else "")
                    + (f"  DROPPED {r['dropped']:,}" if r.get("dropped") else "")
                )
            else:
                print(f"  {r['abbr']:2s}  0")
        except Exception as e:
            print(f"  ERROR {abbr}: {e}")
            import traceback

            traceback.print_exc()


if __name__ == "__main__":
    main()
