#!/usr/bin/env python3
"""Which state actually owns each border cell.

The web demo picks a state file by bounding box, and bounding boxes overlap:
NJ's reaches -73.89, so all of Manhattan is inside it. A 125-city sweep found
25 cities served by a neighbour's file and two -- El Paso and Reno -- drawing
nothing at all. Nearest-centre cannot break the tie either, because NJ's centre
is closer to upper Manhattan than NY's is.

US.admin.ptiles already knows the answer: its aux section is a flat H3 res-7
grid, sorted by cell, with a state index per cell. This writes out the part of
it the demo needs -- only the cells that fall inside two or more state boxes,
because everywhere else the box already answers correctly -- as a small binary
the page can ship with itself instead of fetching 28 MB.

    python3 scripts/build_state_cell_index.py \\
        --admin /mnt/core/timeline-ptiles-cache/tiles/US.admin.ptiles \\
        --index-html ../ptile-client/web-demo/index.html \\
        --out ../ptile-client/web-demo/lib/state_cells.bin

Format (little-endian):

    magic   "PSCI"          4 bytes
    version 1               u8
    nstates                 u8      state codes, 2 ASCII bytes each
    count                   u32     entries, ascending by cell
    entries                 varint(cell - previous cell), u8 state index

Cells are written with the low 21 filler bits cleared, the same normalisation
ptiles_core::normalize_cell applies, so a lookup keyed on a masked cell hits.
"""
import argparse
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from read_admin import decode_all_string_tables, GRID_ENTRY_SIZE  # noqa: E402
from shared import read_header  # noqa: E402

CELL_FILLER_BITS = 0x1F_FFFF  # ptiles_core::CELL_FILLER_BITS

BBOX_RE = re.compile(r"([A-Z]{2}):\[(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)\]")

# The admin grid stores full state names ("Alabama"); every file the demo
# fetches is named by the postal code. Anything not in here (territories,
# Canada, the empty index) is not servable and is dropped.
CODE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


def parse_state_bbox(index_html: str) -> dict:
    """The demo's own STATE_BBOX table, read from the page.

    Re-typing it here would let the two drift, and a table that disagrees with
    the picker it is correcting is worse than no table: it would "fix" cells
    the picker gets right and miss the ones it does not.
    """
    src = open(index_html, encoding="utf-8").read()
    start = src.index("var STATE_BBOX = {")
    end = src.index("};", start)
    out = {}
    for m in BBOX_RE.finditer(src[start:end]):
        st = m.group(1)
        out[st] = tuple(float(m.group(i)) for i in range(2, 6))
    if len(out) < 40:
        raise SystemExit(f"parsed only {len(out)} state boxes from {index_html}")
    return out


def varint(n: int) -> bytes:
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin", default="/mnt/core/timeline-ptiles-cache/tiles/US.admin.ptiles")
    ap.add_argument("--index-html",
                    default="/home/aoi/kino/projects/ptile-client/web-demo/index.html")
    ap.add_argument("--out",
                    default="/home/aoi/kino/projects/ptile-client/web-demo/lib/state_cells.bin")
    ap.add_argument("--all", action="store_true",
                    help="write every cell, not just the ones in two or more boxes")
    args = ap.parse_args()

    import h3
    import numpy as np
    import zstandard as zstd

    boxes = parse_state_bbox(args.index_html)
    print(f"{len(boxes)} state boxes from {args.index_html}")

    with open(args.admin, "rb") as f:
        header = read_header(f)
        f.seek(header["dict_offset"])
        tables = decode_all_string_tables(
            zstd.ZstdDecompressor().decompress(f.read(header["dict_length"])))
        f.seek(header["aux_offset"])
        grid = f.read(header["aux_length"])

    states = tables["state"]
    count = struct.unpack_from("<I", grid, 0)[0]
    print(f"{count:,} grid cells, {len(states)} state names")

    raw = np.frombuffer(grid, dtype=np.uint8, count=count * GRID_ENTRY_SIZE, offset=4)
    raw = raw.reshape(count, GRID_ENTRY_SIZE)
    cells = raw[:, 0:8].copy().view(np.uint64).reshape(count)
    state_idx = raw[:, 9].astype(np.int32)

    # Cell centres, for the box test. h3 has no vectorised call, so this is the
    # slow part -- a few minutes for 1.8M cells, run once per admin build.
    lat = np.empty(count, dtype=np.float64)
    lon = np.empty(count, dtype=np.float64)
    for i in range(count):
        ll = h3.cell_to_latlng(format(int(cells[i]), "x"))
        lat[i], lon[i] = ll[0], ll[1]
        if i % 250_000 == 0:
            print(f"  centres {i:,}/{count:,}", flush=True)

    # ponytail: the centre alone decides ambiguity. A cell whose centre is in
    # one box but whose edge crosses into another is left to the box picker,
    # which is right about it far more often than not -- res-7 cells are ~5 km
    # across and the boxes overlap by tens of kilometres. Test the boundary
    # vertices too if a border city ever turns up missing from this table.
    hits = np.zeros(count, dtype=np.int32)
    for st, (min_lat, min_lon, max_lat, max_lon) in boxes.items():
        hits += ((lat >= min_lat) & (lat <= max_lat)
                 & (lon >= min_lon) & (lon <= max_lon)).astype(np.int32)

    keep = np.ones(count, dtype=bool) if args.all else (hits >= 2)
    # A cell whose state is not one of the boxes (Canada, Mexico, an unnamed
    # index) cannot be routed to a file, so it would only bloat the table.
    names = [CODE.get(states[i], "") if i < len(states) else "" for i in range(256)]
    unknown = sorted({states[i] for i in range(len(states)) if states[i] not in CODE})
    if unknown:
        print(f"  not servable, dropped: {', '.join(unknown)}")
    servable = np.array([1 if names[i] in boxes else 0 for i in range(256)], dtype=bool)
    keep &= servable[state_idx]

    idx = np.flatnonzero(keep)
    print(f"{len(idx):,} cells in two or more boxes and servable "
          f"({100.0 * len(idx) / count:.1f}% of the grid)")

    codes = sorted({names[int(state_idx[i])] for i in idx})
    code_pos = {c: i for i, c in enumerate(codes)}

    masked = (cells[idx] & np.uint64(~CELL_FILLER_BITS & 0xFFFFFFFFFFFFFFFF))
    order = np.argsort(masked, kind="stable")
    masked = masked[order]
    picked = state_idx[idx][order]

    body = bytearray()
    prev = 0
    written = 0
    for cell, si in zip(masked.tolist(), picked.tolist()):
        # Masking collapses neighbouring entries onto the same key only if the
        # grid ever held two cells in one res-7 parent, which it does not --
        # but if it did, the first wins and the rest are noise.
        if cell == prev and written:
            continue
        body += varint(cell - prev)
        body.append(code_pos[names[si]])
        prev = cell
        written += 1

    out = bytearray(b"PSCI")
    out.append(1)
    out.append(len(codes))
    for c in codes:
        out += c.encode("ascii")
    out += struct.pack("<I", written)
    out += body

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(out)
    print(f"wrote {args.out}: {written:,} cells, {len(out) / 1024:.0f} KiB, "
          f"{len(codes)} states")

    # Self-check: decode what was just written and compare against the grid.
    seen = {}
    pos = 6 + 2 * len(codes) + 4
    cell = 0
    for _ in range(written):
        shift, delta = 0, 0
        while True:
            b = out[pos]; pos += 1
            delta |= (b & 0x7F) << shift
            if b < 0x80:
                break
            shift += 7
        cell += delta
        seen[cell] = codes[out[pos]]; pos += 1
    assert pos == len(out), f"decoder consumed {pos} of {len(out)} bytes"
    assert len(seen) == written
    for probe, want in [((40.7580, -73.9855), "NY"), ((36.1627, -86.7816), "TN"),
                        ((31.7619, -106.4850), "TX"), ((39.5296, -119.8138), "NV")]:
        key = int(h3.latlng_to_cell(probe[0], probe[1], 7), 16) & ~CELL_FILLER_BITS
        got = seen.get(key)
        print(f"  {probe} -> {got} (expected {want})"
              + ("" if got == want else "   MISMATCH"))


if __name__ == "__main__":
    main()
