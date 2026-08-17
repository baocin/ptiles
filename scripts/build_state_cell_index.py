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

A res-7 cell is about 5 km across, so a town within a couple of kilometres of
the line sits in a cell the grid assigns to its neighbour -- Southaven,
Jeffersonville, Evansville and El Paso all still routed wrong on the res-7
table alone. For the cells the admin build flagged as straddling a state edge,
the res-9 children (~180 m across) are resolved against the Census state
polygons and written as a second section, so the answer is right to a couple of
hundred metres along every border.

Format (little-endian):

    magic   "PSCI"          4 bytes
    version 2               u8
    nstates                 u8      state codes, 2 ASCII bytes each
    count7                  u32     res-7 entries, ascending by cell
    count9                  u32     res-9 entries, ascending by cell
    entries                 varint(cell - previous cell), u8 state index
                            count7 of them, then count9 of them

Only res-9 children whose state differs from their res-7 parent are written:
the rest are already answered by the coarse section, and storing them would
multiply the table by 49 to say nothing new.

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


STRADDLE_STATE = 0x01  # build_admin.py's boundary_flags bit for a state edge


def refine_border_cells(raw, cells, state_idx, names, boxes, args):
    """res-9 answers for the cells that straddle a state line.

    The res-7 grid is the wrong resolution for a border town: a cell is ~5 km
    across, so Southaven, Jeffersonville, Evansville and El Paso all sit in a
    cell the grid hands to the neighbouring state, and the file that answers has
    none of their ground. The admin build already flags which cells straddle an
    edge, so only those are refined -- ~180 m children, resolved against the
    same Census polygons the admin layer was built from.

    Only children that disagree with their parent are returned. The rest add
    49x the entries to repeat what the coarse section already says.
    """
    import geopandas as gpd
    import h3
    import numpy as np
    import shapely

    if not os.path.exists(args.states_shp):
        print(f"  no state polygons at {args.states_shp}; skipping refinement")
        return []

    flags = raw[:, 15]
    border = np.flatnonzero((flags & STRADDLE_STATE) != 0)
    if not len(border):
        # This admin build left boundary_flags at zero (build_admin.py sets them
        # in a pass that clearly did not run for this file). The grid itself
        # still says where the edges are: a cell whose res-7 neighbour belongs
        # to another state is on one.
        print("  boundary_flags are all zero; finding edges from the grid",
              flush=True)
        owner = {}
        for i in range(len(cells)):
            owner[int(cells[i])] = int(state_idx[i])
        found = []
        for i in range(len(cells)):
            mine = int(state_idx[i])
            if not names[mine]:
                continue
            try:
                ring = h3.grid_disk(format(int(cells[i]), "x"), 1)
            except Exception:
                continue
            for c in ring:
                other = owner.get(int(c, 16))
                if other is not None and other != mine and names[other]:
                    found.append(i)
                    break
            if len(found) % 20_000 == 0 and found and found[-1] == i:
                print(f"  edges {len(found):,} (scanned {i:,}/{len(cells):,})",
                      flush=True)
        border = np.array(found, dtype=np.int64)
    print(f"{len(border):,} cells straddle a state line", flush=True)
    if not len(border):
        return []

    gdf = gpd.read_file(args.states_shp)
    gdf = gdf[gdf["STUSPS"].isin(boxes)]
    geoms = list(gdf.geometry.values)
    owners = list(gdf["STUSPS"].values)
    tree = shapely.STRtree(geoms)

    kids, parent_state = [], []
    for i in border.tolist():
        parent = format(int(cells[i]), "x")
        try:
            children = h3.cell_to_children(parent, 9)
        except Exception:
            continue
        kids.extend(children)
        parent_state.extend([names[int(state_idx[i])]] * len(children))
    print(f"  {len(kids):,} res-9 children to place", flush=True)

    lat = np.empty(len(kids)); lon = np.empty(len(kids))
    for j, c in enumerate(kids):
        ll = h3.cell_to_latlng(c)
        lat[j], lon[j] = ll[0], ll[1]
        if j and j % 500_000 == 0:
            print(f"  centres {j:,}/{len(kids):,}", flush=True)

    pts = shapely.points(lon, lat)
    # `within` rather than nearest: a child centre in the sea or over the border
    # into Canada belongs to nobody, and inventing an owner for it would route
    # the map to a file that does not hold it.
    child_i, geom_i = tree.query(pts, predicate="within")
    out = []
    for ci, gi in zip(child_i.tolist(), geom_i.tolist()):
        owner = owners[gi]
        if owner == parent_state[ci] or owner not in boxes:
            continue
        out.append((int(kids[ci], 16), owner))
    out.sort()
    print(f"  {len(out):,} children disagree with their parent")
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
    ap.add_argument("--no-refine", action="store_true",
                    help="skip the res-9 border pass (coarse cells only)")
    ap.add_argument("--states-shp",
                    default="/mnt/core/timeline-ptiles-cache/admin_data/states/"
                            "cb_2023_us_state_500k.shp")
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

    def encode(pairs):
        """(cell, state code) ascending -> delta-coded bytes, and how many."""
        buf, prev, n = bytearray(), 0, 0
        for cell, code in pairs:
            # Two entries on one key can only happen if the grid held two cells
            # under one parent, which it does not -- but if it did, the first
            # wins and the rest are noise.
            if cell == prev and n:
                continue
            buf += varint(cell - prev)
            buf.append(code_pos[code])
            prev = cell
            n += 1
        return bytes(buf), n

    coarse, written = encode(
        (c, names[s]) for c, s in zip(masked.tolist(), picked.tolist()))

    fine, fine_written = b"", 0
    if not args.no_refine:
        fine_pairs = refine_border_cells(raw, cells, state_idx, names, boxes, args)
        for _, code in fine_pairs:
            if code not in code_pos:
                code_pos[code] = len(codes)
                codes.append(code)
        fine, fine_written = encode(fine_pairs)

    out = bytearray(b"PSCI")
    out.append(2)
    out.append(len(codes))
    for c in codes:
        out += c.encode("ascii")
    out += struct.pack("<I", written)
    out += struct.pack("<I", fine_written)
    out += coarse
    out += fine

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(out)
    print(f"wrote {args.out}: {written:,} res-7 cells, {fine_written:,} res-9 cells, "
          f"{len(out) / 1024:.0f} KiB, {len(codes)} states")

    # Self-check: decode what was just written, the way the page will, and put
    # known points through it. A table that only round-trips its own encoder
    # would still be wrong about the ground.
    pos = 6 + 2 * len(codes) + 8

    def read_section(n):
        nonlocal pos
        got, cell = {}, 0
        for _ in range(n):
            shift, delta = 0, 0
            while True:
                b = out[pos]; pos += 1
                delta |= (b & 0x7F) << shift
                if b < 0x80:
                    break
                shift += 7
            cell += delta
            got[cell] = codes[out[pos]]; pos += 1
        return got

    coarse_map = read_section(written)
    fine_map = read_section(fine_written)
    assert pos == len(out), f"decoder consumed {pos} of {len(out)} bytes"
    assert len(coarse_map) == written and len(fine_map) == fine_written

    bad = 0
    for probe, want in [((40.7580, -73.9855), "NY"),   # Manhattan, inside NJ's box
                        ((31.7619, -106.4850), "TX"),  # El Paso, a mile from NM
                        ((39.5296, -119.8138), "NV"),  # Reno, inside CA's box
                        ((34.9890, -89.9873), "MS"),   # Southaven, 2 km from TN
                        ((38.2775, -85.7372), "IN"),   # Jeffersonville, over the river
                        ((37.9716, -87.5711), "IN"),   # Evansville, likewise
                        ((36.1627, -86.7816), None)]:  # Nashville: one box, not in table
        fine = fine_map.get(int(h3.latlng_to_cell(probe[0], probe[1], 9), 16))
        coarse = coarse_map.get(
            int(h3.latlng_to_cell(probe[0], probe[1], 7), 16) & ~CELL_FILLER_BITS)
        got = fine or coarse
        ok = got == want
        bad += 0 if ok else 1
        print(f"  {probe} -> {got} (res9 {fine}, res7 {coarse}; expected {want})"
              + ("" if ok else "   MISMATCH"))
    if bad:
        raise SystemExit(f"{bad} probe(s) wrong -- not writing this off as noise")


if __name__ == "__main__":
    main()
