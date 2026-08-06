#!/usr/bin/env python3
"""
Upgrade a roads .ptiles file from schema v1 to v2 by appending the
intersection table to every cell block.

A v2 roads file is byte-identical to v1 except:

  1. the header version byte (offset 8) is 2 instead of 1;
  2. every decompressed block gains an intersection table *after* the road
     records, following the zero-length record terminator.

Block layout (matching ptiles/roads.py::decode_block and ptile-client
core/src/roads.rs::decode_road_block):

    { u32 record_len, record_body } *      road records
    u32 0                                  terminator
    u16 count                              intersection table
    count x { i32 lon, i32 lat, u8 type }

Coordinates are degrees * 100_000 -- the field is named `lon_micro` in both
readers, but the scale is the same 1e5 the road records use, NOT 1e6.

Types: 1=traffic_signals, 2=stop, 3=give_way, 4=roundabout.

The intersections are OSM nodes taken straight from US.signals.ptiles, not
topology derived from the road graph. Verified against the shipped
TN.roads.ptiles v2: over its 23,087 blocks, 23,003 tables are an exact
content match for the signals nodes in the same cell filtered to
traffic_signals/stop/give_way (the other 84 are OSM snapshot drift -- TN was
built in May, US.signals in July -- and every one of them is the roads file
having *fewer* nodes, never different ones). Of the cells with no drift,
1,848/1,848 are in signals-record (OSM node id) order, which is what this
script emits. crossing_signals and railway_signals are excluded. Type 4
(roundabout) is not derivable from the signals layer and TN contains none, so
it is never emitted.

TN writes an explicit empty table (u16 0) for cells with no intersections
rather than omitting it -- 3,674 of a 4,000-block sample -- so this script
does the same. The terminator is required either way: the readers stop
scanning records at end-of-input, and without a terminator there is no
position at which to start reading the table.


ROUNDABOUTS (type 4) -- UNVALIDATED DECISIONS
=============================================

Type 4 cannot come from the signals layer: `junction=roundabout` is a WAY tag,
so there is no node to read. It is extracted here directly from the state OSM
PBF instead.

There is NO GROUND TRUTH for any of the choices below. The shipped TN.roads v2
contains zero type-4 entries across all 23,087 of its blocks, so nothing in the
reference data confirms or refutes them. They are DECISIONS, not derivations,
and a later reference file could contradict every one of them.

  1. COORDINATE = the arithmetic mean of the roundabout's node positions.
     Not the circumcentre, not a polygon area centroid, not the node nearest
     that mean. For a closed way the duplicated closing node is dropped first
     so it does not get double weight. A roundabout traced as a near-circle
     puts this mean near the circle's centre, which is the intuitive "where
     the intersection is" answer, but no reference file says so.

  2. GROUPING = connected component, by default (--roundabout-grouping).
     A physical roundabout is frequently NOT one way. Measured on the PBFs:
     DE 283 roundabout ways -> 169 physical roundabouts, DC 123 -> 46 (its
     largest traffic circle is traced as 16 separate ways), HI 231 -> 127.
     Emitting one intersection per WAY would stack 16 points on one DC circle,
     so ways that share an endpoint node are unioned and the component emits a
     single intersection at the mean of all its nodes. `--roundabout-grouping
     way` restores the literal one-per-way reading. Two distinct roundabouts
     joined by a connector that is itself tagged `junction=roundabout` will
     merge into one; that is accepted.

  3. ORDERING = signals nodes first, in their existing OSM-node-id order,
     then roundabouts appended in ascending OSM WAY id (for a component, the
     smallest way id in it). Way ids and node ids are different id spaces, so
     there is no meaningful way to interleave them; appending leaves every
     node-sourced entry exactly where the signals-only build put it and is
     deterministic and reproducible. A cell that gains only roundabouts still
     gets a well-formed table.

  4. CELL = the H3 res-7 cell containing the emitted coordinate, so a
     roundabout can never land outside its own cell by construction.

`junction=circular` is NOT treated as a roundabout. Ways tagged
`junction=roundabout` are taken regardless of their `highway` value (in
practice all of them carry one).


V2 -> V2: APPENDING ROUNDABOUTS TO A FILE THAT ALREADY HAS TABLES
=================================================================

NC and TN shipped as v2 already, with intersection tables carrying types 1/2/3
and zero type 4. `--append-roundabouts` adds the roundabouts to such a file
without touching anything else.

The existing table entries are COPIED BYTE-FOR-BYTE in their existing order and
are never re-derived from US.signals. The shipped tables are an OSM snapshot of
their own build date (TN.roads is 18 May, US.signals 23 Jul) and 84 of TN's
23,087 cells already disagree with what the signals layer would produce today,
so regenerating them would silently rewrite shipped data. The signals file is
not read at all on this path.

Roundabouts are appended after the existing entries, in ascending OSM way id,
exactly as the v1 path appends them after the signals nodes -- so type 4 is
always a contiguous suffix of the table. A cell whose existing table is empty
still ends up with a well-formed table. Road records, the record terminator,
the header, the dictionary, the index entry width, the cell order and every
feature_count carry over untouched; only the per-block offsets/lengths move,
because the blocks are recompressed.


V2 -> V2: REBUILDING THE TABLES FROM THE CURRENT SNAPSHOT
=========================================================

`--rebuild-tables` is the OPPOSITE choice to `--append-roundabouts`: instead of
preserving each block's shipped table, it throws the table away and regenerates
it exactly as the v1 path does -- types 1/2/3 from US.signals, type 4 from the
state PBF.

The reason is snapshot consistency, not correctness of the old data. NC and TN
shipped as v2 from an 18 May OSM extract; the other 49 states get their tables
from the 23 Jul US.signals.ptiles. Measured on TN, the shipped tables hold
12,103 type-1/2/3 entries where the July signals give 12,425 for the same cells
-- 322 entries that exist in every other state's snapshot and not in TN's.
Preserving the shipped table keeps that gap forever; rebuilding closes it, at
the cost of rewriting data that shipped.

Both paths therefore exist and neither is the default. Use
`--append-roundabouts` to hold shipped tables sacred, `--rebuild-tables` to make
all 51 states one snapshot produced by one code path.

This is NOT a separate table builder: `upgrade()` does the work for both v1
inputs and rebuilt v2 inputs, so the entries, their order (signals in OSM node
id order, roundabouts appended in ascending way id) and the encoding are
produced by the same lines of code that produced the other 49 states. The only
difference a v2 input makes is that the block's bytes past the record
terminator are discarded instead of being asserted absent.

Road records stay byte-identical, terminator included -- only the table region
is replaced. Because entries are re-derived per cell from that cell's own
signals records and from a quantise-then-derive-cell roundabout centroid, an
entry can no longer sit outside the block it lives in, which the shipped tables
occasionally did.


Usage:
    # single state
    upgrade_roads_v2.py IN.roads.ptiles OUTDIR/ [--signals US.signals.ptiles]
                        [--pbf STATE.osm.pbf | --pbf-dir DIR | --no-roundabouts]
    upgrade_roads_v2.py IN.roads.ptiles OUTDIR/ --verify-only

    # v2 input: keep its tables, append type 4
    upgrade_roads_v2.py NC.roads.ptiles OUTDIR/ --append-roundabouts \
                        --pbf NC.osm.pbf

    # v2 input: discard its tables, rebuild them from the current snapshot
    upgrade_roads_v2.py NC.roads.ptiles OUTDIR/ --rebuild-tables \
                        --pbf NC.osm.pbf

    # many states in one process: the 13,521-block US.signals file is
    # decompressed ONCE and reused, instead of once per state
    upgrade_roads_v2.py --batch --indir DIR --outdir DIR --pbf-dir DIR
                        [--states AL,AK,...] [--verify-sample N]
"""

from __future__ import annotations

import argparse
import collections
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zstandard as zstd

from ptiles.codec import (
    HEADER_SIZE,
    INDEX_ENTRY_SIZE,
    read_header,
    read_index,
)
from ptiles.compression import compress_block, decompress_block, decompress_fallback

DEFAULT_SIGNALS = Path(__file__).resolve().parent.parent / "tiles" / "US.signals.ptiles"

# H3 ids in an index carry the low 21 bits set; a caller's lookup id may have
# them masked off. Normalise both sides or every cross-file lookup misses
# silently and you get plausible-looking empty tables.
CELL_MASK = ~0x1FFFFF

# ptiles/roads.py::IntersectionType. Signal types with no intersection
# meaning (crossing_signals, railway_signals) are dropped.
SIGNAL_TYPES = ["traffic_signals", "crossing_signals", "stop", "give_way",
                "railway_signals"]
INTERSECTION_TYPE = {"traffic_signals": 1, "stop": 2, "give_way": 3}

INDEX_ENTRY_SIZE_V2 = 38          # US.signals index stride
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MAX_BLOCK_LEN = (1 << 24) - 1     # the index length field is 3 bytes
MAX_TABLE_COUNT = 0xFFFF          # the table count field is u16


# ----------------------------------------------------------------- signals

def _decode_signal_records(body: bytes) -> list[tuple[int, int, str]]:
    """Decode one cell's signals records.

    Mirrors ptile-client core/src/signals.rs and
    scripts/build_points.py::decode_records: back-to-back records with no
    length prefix. Returns (lon, lat, type_name) in file order, which is
    ascending OSM node id.
    """
    out = []
    p = 0
    prev = 0
    while p < len(body):
        val = shift = 0
        while True:
            b = body[p]
            p += 1
            val |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        prev += (val >> 1) ^ -(val & 1)          # zigzag delta osm_id
        lon, lat = struct.unpack_from("<ii", body, p)
        p += 8
        stype = body[p]
        p += 1
        flags = body[p]
        p += 1
        if flags & 0x01:
            p += 2                                # direction u16
        out.append((lon, lat, SIGNAL_TYPES[stype] if stype < len(SIGNAL_TYPES) else ""))
    if p != len(body):
        raise ValueError(f"signals record stream desync: consumed {p} of {len(body)}")
    return out


def load_intersections(signals_path: Path,
                       wanted: set[int] | None = None) -> dict[int, list[tuple]]:
    """Map masked H3 cell -> [(lon, lat, intersection_type), ...].

    US.signals uses a 38-byte index over MERGED blocks: one decompressed
    block holds several cells behind a cell table, and must be sliced before
    its records can be decoded. Slicing it wrong yields plausible garbage
    rather than an error, so this follows build_points.py::verify_file's
    layout exactly.

    `wanted` restricts the result to a set of masked cells (the roads file's
    index), which keeps the map to the state being upgraded.
    """
    with open(signals_path, "rb") as f:
        h = read_header(f)
        f.seek(h["dict_offset"])
        dict_data = f.read(h["dict_length"]) if h["dict_length"] else b""

        f.seek(h["index_offset"])
        count = struct.unpack("<I", f.read(4))[0]
        raw = f.read(count * INDEX_ENTRY_SIZE_V2)

        # Several index entries share one merged block; group by block so
        # each is decompressed once.
        blocks: dict[tuple[int, int], None] = {}
        for i in range(count):
            e = raw[i * INDEX_ENTRY_SIZE_V2:(i + 1) * INDEX_ENTRY_SIZE_V2]
            off = (e[32] << 48) | int.from_bytes(e[24:30], "little")
            ln = (e[33] << 16) | struct.unpack_from("<H", e, 30)[0]
            blocks[(off, ln)] = None

        dctx = (zstd.ZstdDecompressor(dict_data=zstd.ZstdCompressionDict(dict_data))
                if dict_data else zstd.ZstdDecompressor())
        plain = zstd.ZstdDecompressor()

        out: dict[int, list[tuple]] = {}
        for off, ln in blocks:
            f.seek(off)
            blob = f.read(ln)
            try:
                blk = dctx.decompress(blob)
            except Exception:
                blk = plain.decompress(blob)
            _, _, cell_count = struct.unpack_from("<iiI", blk, 0)
            table = [struct.unpack_from("<QI", blk, 12 + 12 * i)
                     for i in range(cell_count)]
            body_start = 12 + 12 * cell_count
            for j, (cell_id, rel) in enumerate(table):
                masked = cell_id & CELL_MASK
                if wanted is not None and masked not in wanted:
                    continue
                lo = body_start + rel
                hi = (body_start + table[j + 1][1] if j + 1 < cell_count
                      else len(blk))
                ints = [(lon, lat, INTERSECTION_TYPE[t])
                        for lon, lat, t in _decode_signal_records(blk[lo:hi])
                        if t in INTERSECTION_TYPE]
                if ints:
                    out[masked] = ints
    return out


# ------------------------------------------------------------- roundabouts

ROUNDABOUT_TYPE = 4


def _pbf_candidates(state_abbr: str, state_name: str) -> list[str]:
    """Filenames a state's PBF might have, most specific first.

    The shipped cache uses hyphenated full lowercase names
    (`rhode-island.osm.pbf`, `district-of-columbia.osm.pbf`); underscored and
    abbreviation forms are accepted too so a locally staged copy also works.
    """
    low = state_name.lower()
    return [f"{state_abbr}.osm.pbf",
            f"{low.replace(' ', '-')}.osm.pbf",
            f"{low.replace(' ', '_')}.osm.pbf",
            f"{low.replace(' ', '')}.osm.pbf"]


def find_pbf(pbf_dir: Path, state_abbr: str, state_name: str) -> Path:
    """Locate a state's PBF or raise. Never silently skips a missing state."""
    for name in _pbf_candidates(state_abbr, state_name):
        p = pbf_dir / name
        if p.exists():
            return p
    raise FileNotFoundError(
        f"no PBF for {state_abbr} ({state_name}) in {pbf_dir}; tried "
        + ", ".join(_pbf_candidates(state_abbr, state_name)))


def _read_roundabout_ways(pbf_path: Path) -> tuple[list[tuple[int, list, list]], int]:
    """One osmium pass: ([(way_id, node_refs, coords), ...], dropped_count).

    Needs node locations, so the handler is applied with locations=True (the
    same way build_roads.py gets way geometry). Roundabout ways are a tiny
    fraction of a state, so holding all of them is cheap; only the location
    index is large.
    """
    import osmium

    class _Handler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.ways = []
            self.dropped = 0

        def way(self, w):
            if w.tags.get("junction") != "roundabout":
                return
            try:
                coords = [(n.lon, n.lat) for n in w.nodes]
                refs = [n.ref for n in w.nodes]
            except osmium.InvalidLocationError:
                self.dropped += 1
                return
            if not coords:
                self.dropped += 1
                return
            self.ways.append((w.id, refs, coords))

    h = _Handler()
    h.apply_file(str(pbf_path), locations=True)
    return h.ways, h.dropped


def _group_components(ways: list[tuple[int, list, list]]) -> list[list[int]]:
    """Union roundabout ways that share an endpoint node.

    Returns a list of components, each a list of indices into `ways`. Arcs of
    one physical roundabout are chained end-to-end, so endpoint sharing is what
    reassembles them; sharing an interior node is not enough to be the same
    roundabout.
    """
    parent = list(range(len(ways)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    endpoint_owner: dict[int, int] = {}
    for i, (_wid, refs, _c) in enumerate(ways):
        for r in (refs[0], refs[-1]):
            j = endpoint_owner.get(r)
            if j is not None:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
            endpoint_owner[r] = i

    groups: dict[int, list[int]] = {}
    for i in range(len(ways)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _mean_position(coords_lists: list[list]) -> tuple[float, float]:
    """Arithmetic mean of node positions, dropping each way's closing node.

    A closed way repeats its first node last; counting it twice biases the mean
    toward that node.
    """
    slon = slat = 0.0
    n = 0
    for coords in coords_lists:
        pts = coords[:-1] if len(coords) > 2 and coords[0] == coords[-1] else coords
        for lon, lat in pts:
            slon += lon
            slat += lat
            n += 1
    if not n:
        raise ValueError("roundabout with no usable nodes")
    return slon / n, slat / n


def extract_roundabouts(pbf_path: Path, grouping: str = "component"
                        ) -> tuple[dict[int, list[tuple]], dict]:
    """Returns ({masked H3 res-7 cell: [(lon, lat, 4, sort_key), ...]}, stats).

    See the module docstring: the coordinate, the grouping and the ordering key
    are all UNVALIDATED decisions -- the reference TN v2 file contains no
    roundabouts to check them against.
    """
    import h3

    ways, dropped = _read_roundabout_ways(pbf_path)
    if grouping == "way":
        components = [[i] for i in range(len(ways))]
    elif grouping == "component":
        components = _group_components(ways)
    else:
        raise ValueError(f"unknown roundabout grouping {grouping!r}")

    out: dict[int, list[tuple]] = {}
    for comp in components:
        try:
            lon, lat = _mean_position([ways[i][2] for i in comp])
        except ValueError:
            dropped += len(comp)
            continue
        # Sort key: the smallest OSM way id in the component.
        key = min(ways[i][0] for i in comp)
        # Quantise FIRST, then pick the cell from the quantised value. The
        # stored coordinate is degrees*1e5, and a centroid that sits within
        # half a unit of a cell boundary can round across it -- picking the
        # cell from the unrounded centroid then files the entry in a cell that
        # does not contain the point a reader will decode. Observed once in NV
        # (a roundabout at 36.12495,-115.26783 landed one cell over).
        ilon = int(round(lon * 100_000))
        ilat = int(round(lat * 100_000))
        cell = h3.str_to_int(
            h3.latlng_to_cell(ilat / 1e5, ilon / 1e5, 7)) & CELL_MASK
        out.setdefault(cell, []).append((ilon, ilat, ROUNDABOUT_TYPE, key))

    for v in out.values():
        v.sort(key=lambda t: t[3])
    return out, {"ways": len(ways), "roundabouts": sum(len(v) for v in out.values()),
                 "dropped": dropped}


# ------------------------------------------------------------------- roads

def scan_records(data: bytes) -> tuple[int, int, bool]:
    """Scan a decompressed roads block's record region.

    Returns (record_count, end_offset, had_terminator). `end_offset` is the
    byte after the terminating zero-length record, or the point at which the
    records ran out. Mirrors core/src/roads.rs::decode_road_records.
    """
    p = 0
    n = 0
    while p + 4 <= len(data):
        rlen = struct.unpack_from("<I", data, p)[0]
        p += 4
        if rlen == 0:
            return n, p, True
        if p + rlen > len(data):
            raise ValueError(f"record overrun at {p}: len {rlen} > block {len(data)}")
        p += rlen
        n += 1
    return n, p, False


def encode_table(ints: list[tuple[int, int, int]]) -> bytes:
    """Encode an intersection table: u16 count + count x (i32,i32,u8)."""
    if len(ints) > MAX_TABLE_COUNT:
        ints = ints[:MAX_TABLE_COUNT]
    buf = bytearray(struct.pack("<H", len(ints)))
    for lon, lat, itype in ints:
        buf += struct.pack("<iiB", lon, lat, itype)
    return bytes(buf)


def write_file(out_path: Path, header_bytes: bytes, h: dict, aux: bytes,
               dict_data: bytes, index: list, new_blocks: list[bytes],
               relative: bool) -> None:
    """Lay a rewritten file back out with the version byte forced to 2.

    dict/index positions and the index entry width are unchanged, so
    dict_offset, index_offset, index_length and blocks_offset all carry over
    untouched; only the per-block offsets and lengths inside the index move.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    entries = bytearray()
    cursor = 0
    for e, blk in zip(index, new_blocks):
        if len(blk) > MAX_BLOCK_LEN:
            raise SystemExit(
                f"cell {e['h3_cell']:#x}: recompressed block is {len(blk)} bytes, "
                f"over the {MAX_BLOCK_LEN}-byte index length field")
        abs_off = h["blocks_offset"] + cursor
        stored = cursor if relative else abs_off
        # Re-emit the entry with only offset and length changed; h3_cell and
        # feature_count carry over verbatim.
        entries += struct.pack("<Q", e["h3_cell"])
        entries += stored.to_bytes(6, "little")
        entries += len(blk).to_bytes(3, "little")
        entries += struct.pack("<H", min(e["feature_count"], 0xFFFF))
        cursor += len(blk)

    with open(out_path, "wb") as out:
        hb = bytearray(header_bytes)
        hb[8] = 2                                  # version 1 -> 2 (2 -> 2)
        out.write(bytes(hb))
        if aux:
            out.seek(h["aux_offset"])
            out.write(aux)
        out.seek(h["dict_offset"])
        out.write(dict_data)
        out.seek(h["index_offset"])
        out.write(struct.pack("<I", len(index)))
        out.write(bytes(entries))
        if out.tell() != h["blocks_offset"]:
            # Same entry count and width in, same out -- this cannot drift
            # unless the input's own header disagreed with its index.
            raise SystemExit(
                f"index ends at {out.tell()} but header says blocks start at "
                f"{h['blocks_offset']}")
        for blk in new_blocks:
            out.write(blk)


def upgrade(in_path: Path, out_path: Path,
            cellmap: dict[int, list[tuple]],
            roundmap: dict[int, list[tuple]] | None = None,
            level: int = 12,
            rebuild_from_v2: bool = False) -> dict:
    """Rewrite a v1 roads file as v2. Returns a stats dict.

    `cellmap` and `roundmap` are prebuilt (masked cell -> entries) maps. The
    signals map covers the whole US and is built ONCE by the caller -- loading
    it costs ~2 s and 13,521 block decompressions, which is per-run work, not
    per-state work. Cells outside this state are simply never looked up.

    `rebuild_from_v2` accepts a v2 input and DISCARDS its existing intersection
    table, regenerating it from the same `cellmap`/`roundmap` as a v1 input --
    that is the entire difference between the two paths, and it is why the
    rebuilt states come out of the same code as the other 49. The road records
    and their terminator are untouched either way; only the bytes past the
    terminator are dropped. See the module docstring. The counts of the
    discarded entries are reported as `old_type_N`/`old_entries` so a caller can
    print the before/after.
    """
    t0 = time.time()
    stats: collections.Counter = collections.Counter()

    with open(in_path, "rb") as f:
        header_bytes = f.read(HEADER_SIZE)
        f.seek(0)
        h = read_header(f)
        want = 2 if rebuild_from_v2 else 1
        if h["version"] != want:
            if h["version"] == 2 and not rebuild_from_v2:
                raise SystemExit(
                    f"{in_path} is already version 2; use --rebuild-tables to "
                    "regenerate its tables or --append-roundabouts to keep them")
            raise SystemExit(
                f"{in_path} is version {h['version']}, expected {want}")
        if not h["magic"].startswith(b"PTILESR"):
            raise SystemExit(f"{in_path} is not a roads file (magic {h['magic']!r})")

        f.seek(h["dict_offset"])
        dict_data = f.read(h["dict_length"]) if h["dict_length"] else b""

        f.seek(h["index_offset"])
        index_raw = f.read(h["index_length"])
        index = read_index(index_raw)

        stride = (h["index_length"] - 4) / len(index) if index else 0
        if index and abs(stride - INDEX_ENTRY_SIZE) > 1e-9:
            raise SystemExit(
                f"unexpected index stride {stride} (expected {INDEX_ENTRY_SIZE}); "
                "this script preserves the input's entry width and will not guess")

        # Offset base convention. Preserved as found, never silently changed.
        relative = bool(index) and index[0]["block_offset"] < h["blocks_offset"]

        aux = b""
        if h["aux_length"]:
            f.seek(h["aux_offset"])
            aux = f.read(h["aux_length"])

        roundmap = roundmap or {}
        wanted = {e["h3_cell"] & CELL_MASK for e in index}
        stats["cells_with_signals"] = sum(1 for c in wanted if c in cellmap)
        stats["cells_with_roundabouts"] = sum(1 for c in wanted if c in roundmap)
        # A roundabout whose cell has no roads block has nowhere to live; the
        # index is fixed by the v1 input and no block may be added to it.
        stats["roundabouts_no_block"] = sum(
            len(v) for c, v in roundmap.items() if c not in wanted)

        # Re-encode every block.
        new_blocks: list[bytes] = []
        for e in index:
            src = h["blocks_offset"] + e["block_offset"] if relative else e["block_offset"]
            f.seek(src)
            blob = f.read(e["block_length"])

            raw = decompress_block(blob, dict_data) if dict_data else None
            if raw is None:
                raw = decompress_fallback(blob)
                if raw is None:
                    raise SystemExit(f"cell {e['h3_cell']:#x}: block will not decompress")
                stats["plain_blocks"] += 1

            n, end, had_term = scan_records(raw)
            stats["roads"] += n
            if rebuild_from_v2:
                if not had_term:
                    raise SystemExit(
                        f"cell {e['h3_cell']:#x}: v2 block has no record terminator")
                # Decode the table only to count what is being discarded and to
                # confirm the input really is a well-formed v2 block; the bytes
                # themselves are dropped.
                old_count, old_entries = read_table(raw, end, e["h3_cell"])
                stats["old_entries"] += old_count
                for i in range(old_count):
                    stats[f"old_type_{old_entries[9 * i + 8]}"] += 1
                # Everything up to and including the terminator survives
                # verbatim; the old table region does not.
                body = raw[:end]
            elif had_term:
                stats["already_terminated"] += 1
                if end != len(raw):
                    # A v1 file should have nothing past the terminator. If it
                    # does, it is not a v1 block and we would be corrupting it.
                    raise SystemExit(
                        f"cell {e['h3_cell']:#x}: {len(raw) - end} bytes past the "
                        "record terminator in a v1 file")
                body = raw
            else:
                if end != len(raw):
                    raise SystemExit(
                        f"cell {e['h3_cell']:#x}: {len(raw) - end} trailing bytes "
                        "that are not a record")
                body = raw + b"\x00\x00\x00\x00"

            masked = e["h3_cell"] & CELL_MASK
            # Ordering: signals nodes keep their OSM-node-id order, then
            # roundabouts appended in ascending OSM way id. See the docstring.
            ints = list(cellmap.get(masked, ()))
            ints += [(lon, lat, t) for lon, lat, t, _key in roundmap.get(masked, ())]
            stats["intersections"] += len(ints)
            for _lon, _lat, t in ints:
                stats[f"type_{t}"] += 1
            if ints:
                stats["blocks_with_table"] += 1
            else:
                stats["blocks_empty_table"] += 1
            new_blocks.append(compress_block(body + encode_table(ints),
                                             dict_data, level=level))

    write_file(out_path, header_bytes, h, aux, dict_data, index, new_blocks,
               relative)

    stats["blocks"] = len(index)
    stats["seconds"] = round(time.time() - t0, 1)
    stats["in_bytes"] = in_path.stat().st_size
    stats["out_bytes"] = out_path.stat().st_size
    return dict(stats)


def read_table(raw: bytes, end: int, cell: int) -> tuple[int, bytes]:
    """Read an existing block's intersection table: (count, raw entry bytes).

    `end` is the offset just past the record terminator. The entry bytes are
    returned verbatim so they can be re-emitted without being decoded and
    re-encoded -- a decode/encode round trip is where a shipped table would
    quietly change.
    """
    if end + 2 > len(raw):
        raise SystemExit(f"cell {cell:#x}: no intersection table in a v2 block")
    count = struct.unpack_from("<H", raw, end)[0]
    body = raw[end + 2:]
    if len(body) != 9 * count:
        raise SystemExit(
            f"cell {cell:#x}: table declares {count} entries but {len(body)} "
            f"bytes follow (expected {9 * count})")
    return count, body


def append_roundabouts(in_path: Path, out_path: Path,
                       roundmap: dict[int, list[tuple]],
                       level: int = 12,
                       allow_existing_type4: bool = False) -> dict:
    """v2 -> v2: append type-4 entries to a file that already has tables.

    The existing entries are copied byte-for-byte in their existing order (see
    the module docstring); they are never re-derived. Roundabouts land after
    them, ascending OSM way id, so type 4 is a contiguous suffix.

    Refuses on an input that already contains a type-4 entry, unless
    `allow_existing_type4` -- appending to a table that already has roundabouts
    would either duplicate them or break the suffix invariant, and there is no
    correct silent answer.
    """
    t0 = time.time()
    stats: collections.Counter = collections.Counter()

    with open(in_path, "rb") as f:
        header_bytes = f.read(HEADER_SIZE)
        f.seek(0)
        h = read_header(f)
        if h["version"] != 2:
            raise SystemExit(
                f"{in_path} is version {h['version']}, expected 2; use the "
                "v1 path (no --append-roundabouts) for a v1 file")
        if not h["magic"].startswith(b"PTILESR"):
            raise SystemExit(f"{in_path} is not a roads file (magic {h['magic']!r})")

        f.seek(h["dict_offset"])
        dict_data = f.read(h["dict_length"]) if h["dict_length"] else b""

        f.seek(h["index_offset"])
        index = read_index(f.read(h["index_length"]))

        stride = (h["index_length"] - 4) / len(index) if index else 0
        if index and abs(stride - INDEX_ENTRY_SIZE) > 1e-9:
            raise SystemExit(
                f"unexpected index stride {stride} (expected {INDEX_ENTRY_SIZE}); "
                "this script preserves the input's entry width and will not guess")

        relative = bool(index) and index[0]["block_offset"] < h["blocks_offset"]

        aux = b""
        if h["aux_length"]:
            f.seek(h["aux_offset"])
            aux = f.read(h["aux_length"])

        wanted = {e["h3_cell"] & CELL_MASK for e in index}
        stats["cells_with_roundabouts"] = sum(1 for c in wanted if c in roundmap)
        stats["roundabouts_no_block"] = sum(
            len(v) for c, v in roundmap.items() if c not in wanted)

        new_blocks: list[bytes] = []
        for e in index:
            src = h["blocks_offset"] + e["block_offset"] if relative else e["block_offset"]
            f.seek(src)
            blob = f.read(e["block_length"])

            raw = decompress_block(blob, dict_data) if dict_data else None
            if raw is None:
                raw = decompress_fallback(blob)
                if raw is None:
                    raise SystemExit(f"cell {e['h3_cell']:#x}: block will not decompress")
                stats["plain_blocks"] += 1

            n, end, had_term = scan_records(raw)
            stats["roads"] += n
            if not had_term:
                raise SystemExit(
                    f"cell {e['h3_cell']:#x}: v2 block has no record terminator")

            old_count, old_entries = read_table(raw, end, e["h3_cell"])
            for i in range(old_count):
                stats[f"type_{old_entries[9 * i + 8]}"] += 1
            stats["existing_entries"] += old_count
            if not allow_existing_type4:
                for i in range(old_count):
                    if old_entries[9 * i + 8] == ROUNDABOUT_TYPE:
                        raise SystemExit(
                            f"cell {e['h3_cell']:#x}: input already has a type-4 "
                            "entry; appending would duplicate it")

            masked = e["h3_cell"] & CELL_MASK
            new = [(lon, lat, t) for lon, lat, t, _k in roundmap.get(masked, ())]
            stats["appended"] += len(new)
            stats[f"type_{ROUNDABOUT_TYPE}"] += len(new)
            total = old_count + len(new)
            if total > MAX_TABLE_COUNT:
                raise SystemExit(
                    f"cell {e['h3_cell']:#x}: {total} entries overflows the u16 "
                    "table count")
            stats["intersections"] += total
            if total:
                stats["blocks_with_table"] += 1
            else:
                stats["blocks_empty_table"] += 1

            body = bytearray(raw[:end])
            body += struct.pack("<H", total)
            body += old_entries                      # verbatim, in order
            for lon, lat, itype in new:
                body += struct.pack("<iiB", lon, lat, itype)
            new_blocks.append(compress_block(bytes(body), dict_data, level=level))

    write_file(out_path, header_bytes, h, aux, dict_data, index, new_blocks,
               relative)

    stats["blocks"] = len(index)
    stats["seconds"] = round(time.time() - t0, 1)
    stats["in_bytes"] = in_path.stat().st_size
    stats["out_bytes"] = out_path.stat().st_size
    return dict(stats)


# ---------------------------------------------------------------- verifying

def _open_ptiles(p: Path):
    """(file, header, dict, index, relative-offsets?) for a roads file."""
    f = open(p, "rb")
    h = read_header(f)
    f.seek(h["dict_offset"])
    dd = f.read(h["dict_length"]) if h["dict_length"] else b""
    f.seek(h["index_offset"])
    idx = read_index(f.read(h["index_length"]))
    rel = bool(idx) and idx[0]["block_offset"] < h["blocks_offset"]
    return f, h, dd, idx, rel


def _read_blk(f, h, dd, e, rel) -> bytes | None:
    off = h["blocks_offset"] + e["block_offset"] if rel else e["block_offset"]
    f.seek(off)
    blob = f.read(e["block_length"])
    raw = decompress_block(blob, dd) if dd else None
    return raw if raw is not None else decompress_fallback(blob)


def verify(v1_path: Path, v2_path: Path, sample: int | None = None) -> list[str]:
    """Compare a v1 file with its upgraded v2 twin. Returns a list of problems.

    The road records must be byte-identical: everything before the terminator
    in the v2 block must equal the whole of the v1 block.
    """
    problems: list[str] = []

    f1, h1, d1, i1, r1 = _open_ptiles(v1_path)
    f2, h2, d2, i2, r2 = _open_ptiles(v2_path)

    if h2["version"] != 2:
        problems.append(f"output version is {h2['version']}, not 2")
    if d1 != d2:
        problems.append("dictionary differs")
    if len(i1) != len(i2):
        problems.append(f"block count {len(i1)} -> {len(i2)}")
    for k in ("dict_offset", "dict_length", "index_offset", "index_length",
              "blocks_offset", "feature_count", "block_count"):
        if h1[k] != h2[k]:
            problems.append(f"header {k}: {h1[k]} -> {h2[k]}")
    if r1 != r2:
        problems.append(f"offset base convention changed ({r1} -> {r2})")

    read_blk = _read_blk

    pairs = list(zip(i1, i2))
    if sample:
        pairs = pairs[::max(1, len(pairs) // sample)][:sample]

    try:
        import h3
    except ImportError:
        h3 = None

    total_ints = 0
    outside = 0
    by_type: collections.Counter = collections.Counter()
    for e1, e2 in pairs:
        if e1["h3_cell"] != e2["h3_cell"]:
            problems.append(f"cell order changed: {e1['h3_cell']:#x} vs {e2['h3_cell']:#x}")
            break
        if e1["feature_count"] != e2["feature_count"]:
            problems.append(f"cell {e1['h3_cell']:#x}: feature_count changed")
        b1 = read_blk(f1, h1, d1, e1, r1)
        b2 = read_blk(f2, h2, d2, e2, r2)
        if b1 is None or b2 is None:
            problems.append(f"cell {e1['h3_cell']:#x}: decompress failed")
            continue
        n1, end1, term1 = scan_records(b1)
        n2, end2, term2 = scan_records(b2)
        if not term2:
            problems.append(f"cell {e1['h3_cell']:#x}: v2 block has no terminator")
            continue
        if n1 != n2:
            problems.append(f"cell {e1['h3_cell']:#x}: {n1} roads -> {n2}")
        keep = end2 - 4 if term2 else end2
        if b2[:keep] != b1[:end1]:
            problems.append(f"cell {e1['h3_cell']:#x}: road record bytes differ")
        if end2 + 2 > len(b2):
            problems.append(f"cell {e1['h3_cell']:#x}: no intersection table")
            continue
        cnt = struct.unpack_from("<H", b2, end2)[0]
        total_ints += cnt
        if end2 + 2 + 9 * cnt != len(b2):
            problems.append(
                f"cell {e1['h3_cell']:#x}: table declares {cnt} but "
                f"{len(b2) - end2 - 2} bytes follow")
        for i in range(cnt):
            off = end2 + 2 + 9 * i
            lon, lat = struct.unpack_from("<ii", b2, off)
            t = b2[off + 8]
            if t not in (1, 2, 3, 4):
                problems.append(f"cell {e1['h3_cell']:#x}: bad intersection type {t}")
                break
            by_type[t] += 1
            # An intersection in the wrong block is invisible to a reader that
            # looks it up by cell, so this is checked, not assumed.
            if h3 is not None:
                got = h3.str_to_int(
                    h3.latlng_to_cell(lat / 1e5, lon / 1e5, 7)) & CELL_MASK
                if got != (e1["h3_cell"] & CELL_MASK):
                    outside += 1
                    if outside <= 3:
                        problems.append(
                            f"cell {e1['h3_cell']:#x}: type-{t} intersection at "
                            f"{lat/1e5:.5f},{lon/1e5:.5f} belongs to {got:#x}")

    f1.close()
    f2.close()
    print(f"  verified {len(pairs)} blocks, {total_ints} intersections "
          f"(by type {dict(sorted(by_type.items()))}), {outside} outside own cell")
    return problems


def verify_v2(old_path: Path, new_path: Path,
              sample: int | None = None,
              preserve_existing: bool = True) -> list[str]:
    """Compare a v2 input with its v2 output. Covers both v2 -> v2 modes.

    Stricter than verify() about the roads either way: the whole record region
    INCLUDING the terminator must be byte-identical, since neither v2 path has
    any business touching a byte before the table.

    `preserve_existing=True` (--append-roundabouts) additionally requires the
    shipped table's entry bytes to survive verbatim and in place, with anything
    added being type 4 sitting after every pre-existing entry -- that is the
    load-bearing claim of that path.

    `preserve_existing=False` (--rebuild-tables) makes the opposite claim: the
    table is regenerated, so its entries are NOT compared with the input's and
    the table is free to shrink. What is checked instead is that the result has
    the shape the v1 path produces -- types 1/2/3 first, type 4 a contiguous
    suffix -- and that every entry lies in its own cell, which a per-cell
    rebuild has no excuse for getting wrong.
    """
    problems: list[str] = []
    f1, h1, d1, i1, r1 = _open_ptiles(old_path)
    f2, h2, d2, i2, r2 = _open_ptiles(new_path)

    if h1["version"] != 2:
        problems.append(f"input version is {h1['version']}, not 2")
    if h2["version"] != 2:
        problems.append(f"output version is {h2['version']}, not 2")
    if d1 != d2:
        problems.append("dictionary differs")
    if len(i1) != len(i2):
        problems.append(f"block count {len(i1)} -> {len(i2)}")
    for k in ("dict_offset", "dict_length", "index_offset", "index_length",
              "blocks_offset", "feature_count", "block_count"):
        if h1[k] != h2[k]:
            problems.append(f"header {k}: {h1[k]} -> {h2[k]}")
    if r1 != r2:
        problems.append(f"offset base convention changed ({r1} -> {r2})")

    pairs = list(zip(i1, i2))
    if sample:
        pairs = pairs[::max(1, len(pairs) // sample)][:sample]

    try:
        import h3
    except ImportError:
        h3 = None

    kept = added = replaced = outside = outside_kept = 0
    by_type: collections.Counter = collections.Counter()
    old_by_type: collections.Counter = collections.Counter()
    for e1, e2 in pairs:
        if e1["h3_cell"] != e2["h3_cell"]:
            problems.append(f"cell order changed: {e1['h3_cell']:#x} vs {e2['h3_cell']:#x}")
            break
        if e1["feature_count"] != e2["feature_count"]:
            problems.append(f"cell {e1['h3_cell']:#x}: feature_count changed")
        b1 = _read_blk(f1, h1, d1, e1, r1)
        b2 = _read_blk(f2, h2, d2, e2, r2)
        if b1 is None or b2 is None:
            problems.append(f"cell {e1['h3_cell']:#x}: decompress failed")
            continue
        n1, end1, t1 = scan_records(b1)
        n2, end2, t2 = scan_records(b2)
        if not (t1 and t2):
            problems.append(f"cell {e1['h3_cell']:#x}: missing record terminator")
            continue
        if n1 != n2:
            problems.append(f"cell {e1['h3_cell']:#x}: {n1} roads -> {n2}")
        if end1 != end2 or b1[:end1] != b2[:end2]:
            problems.append(f"cell {e1['h3_cell']:#x}: road record bytes differ")
            continue

        try:
            c1, ents1 = read_table(b1, end1, e1["h3_cell"])
            c2, ents2 = read_table(b2, end2, e2["h3_cell"])
        except SystemExit as exc:
            problems.append(str(exc))
            continue
        for i in range(c1):
            old_by_type[ents1[9 * i + 8]] += 1
        if preserve_existing:
            if c2 < c1:
                problems.append(f"cell {e1['h3_cell']:#x}: table shrank {c1} -> {c2}")
                continue
            # The load-bearing check: the pre-existing entries survive verbatim
            # and in place, so nothing shipped is re-derived or reordered.
            if ents2[:9 * c1] != ents1:
                problems.append(
                    f"cell {e1['h3_cell']:#x}: existing table entries changed")
            kept += c1
            added += c2 - c1
        else:
            # Nothing is preserved on this path, so there is no "kept" count and
            # no comparison to make: every entry in the output is new. c1 is
            # still read, to report what was replaced and to prove the input
            # parsed as a v2 block.
            replaced += c1
            added += c2
        first4 = None
        for i in range(c2):
            off = 9 * i
            lon, lat = struct.unpack_from("<ii", ents2, off)
            t = ents2[off + 8]
            if t not in (1, 2, 3, 4):
                problems.append(f"cell {e1['h3_cell']:#x}: bad intersection type {t}")
                break
            by_type[t] += 1
            if t == ROUNDABOUT_TYPE:
                if first4 is None:
                    first4 = i
                if preserve_existing and i < c1:
                    problems.append(
                        f"cell {e1['h3_cell']:#x}: type 4 at index {i} is inside "
                        "the pre-existing entries")
            elif first4 is not None:
                problems.append(
                    f"cell {e1['h3_cell']:#x}: type {t} at index {i} follows a "
                    "type 4 -- roundabouts are not a suffix")
            if h3 is not None:
                got = h3.str_to_int(
                    h3.latlng_to_cell(lat / 1e5, lon / 1e5, 7)) & CELL_MASK
                if got != (e1["h3_cell"] & CELL_MASK):
                    # An entry the INPUT already filed in the wrong cell is
                    # reported but is not a failure of an append run: preserving
                    # the shipped table byte-for-byte is the whole point of that
                    # path, and "fix it" would mean moving shipped data. Only an
                    # entry the run itself wrote is our bug -- and on a rebuild
                    # every entry is one, so nothing is excused there.
                    if preserve_existing and i < c1:
                        outside_kept += 1
                    else:
                        outside += 1
                        if outside <= 3:
                            problems.append(
                                f"cell {e1['h3_cell']:#x}: appended type-{t} "
                                f"intersection at {lat/1e5:.5f},{lon/1e5:.5f} "
                                f"belongs to {got:#x}")

    f1.close()
    f2.close()
    if preserve_existing:
        print(f"  verified {len(pairs)} blocks, {kept} entries preserved, "
              f"{added} appended (by type {dict(sorted(by_type.items()))}), "
              f"{outside} appended entries outside own cell")
        if outside_kept:
            print(f"  note: {outside_kept} PRE-EXISTING "
                  f"{'entry' if outside_kept == 1 else 'entries'} already sat "
                  "outside its own cell in the input; kept as is")
    else:
        print(f"  verified {len(pairs)} blocks, {replaced} entries discarded "
              f"(by type {dict(sorted(old_by_type.items()))}), {added} rebuilt "
              f"(by type {dict(sorted(by_type.items()))}), "
              f"{outside} outside own cell")
    return problems


def verify_append(old_path: Path, new_path: Path,
                  sample: int | None = None) -> list[str]:
    """v2 -> v2 append: the shipped tables must survive verbatim."""
    return verify_v2(old_path, new_path, sample=sample, preserve_existing=True)


def verify_rebuild(old_path: Path, new_path: Path,
                   sample: int | None = None) -> list[str]:
    """v2 -> v2 rebuild: the tables are regenerated, the roads are not."""
    return verify_v2(old_path, new_path, sample=sample, preserve_existing=False)


# ------------------------------------------------------------------- driver

def _state_table() -> dict[str, str]:
    """abbr -> full name, from scripts/states.py (50 states + DC)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from states import STATES
    return {s.abbr: s.name for s in STATES}


def _fmt_types(st: dict, prefix: str = "") -> str:
    names = {1: "signals", 2: "stop", 3: "give_way", 4: "roundabout"}
    return " ".join(f"{names[t]}={st.get(f'{prefix}type_{t}', 0)}"
                    for t in (1, 2, 3, 4))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, nargs="?", help="a v1 .roads.ptiles file")
    ap.add_argument("outdir", type=Path, nargs="?",
                    help="output directory; the file keeps its name")
    ap.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS,
                    help=f"node source (default {DEFAULT_SIGNALS})")
    ap.add_argument("--level", type=int, default=12, help="zstd level (default 12)")
    ap.add_argument("--verify-only", action="store_true",
                    help="verify an existing output instead of rebuilding")
    ap.add_argument("--sample", type=int, default=None,
                    help="verify only N blocks (default: all)")
    ap.add_argument("--pbf", type=Path, default=None,
                    help="this state's .osm.pbf, for roundabouts")
    ap.add_argument("--pbf-dir", type=Path, default=None,
                    help="directory of state PBFs; the name is derived from the "
                         "state abbreviation in the input filename")
    ap.add_argument("--no-roundabouts", action="store_true",
                    help="skip type 4 entirely (no PBF needed)")
    ap.add_argument("--append-roundabouts", action="store_true",
                    help="v2 -> v2: the input already has intersection tables; "
                         "keep them byte-for-byte and append type 4 only. "
                         "US.signals is not read on this path")
    ap.add_argument("--rebuild-tables", action="store_true",
                    help="v2 -> v2: the input already has intersection tables; "
                         "DISCARD them and rebuild from US.signals + the PBF, "
                         "exactly as the v1 path does, so the output matches "
                         "the other states' snapshot instead of its own")
    ap.add_argument("--allow-existing-type4", action="store_true",
                    help="--append-roundabouts: proceed even if the input "
                         "already contains roundabouts (they will be duplicated)")
    ap.add_argument("--roundabout-grouping", choices=("component", "way"),
                    default="component",
                    help="'component' (default) emits one intersection per "
                         "physical roundabout, unioning ways that share an "
                         "endpoint; 'way' emits one per OSM way")
    # batch
    ap.add_argument("--batch", action="store_true",
                    help="process many states in one process, building the "
                         "US.signals map once")
    ap.add_argument("--indir", type=Path, default=None, help="batch: v1 input dir")
    ap.add_argument("--outdir", dest="outdir_opt", type=Path, default=None,
                    help="batch: output dir (same as the positional outdir)")
    ap.add_argument("--states", default=None,
                    help="batch: comma-separated abbreviations (default: all 51)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="batch: leave states that already have an output file "
                         "alone, so an interrupted run can be resumed")
    a = ap.parse_args()

    if a.outdir_opt is not None:
        a.outdir = a.outdir_opt
    if a.append_roundabouts and a.no_roundabouts:
        ap.error("--append-roundabouts with --no-roundabouts would do nothing")
    if a.append_roundabouts and a.rebuild_tables:
        ap.error("--append-roundabouts preserves the existing tables and "
                 "--rebuild-tables discards them; pick one")
    if a.batch:
        return _main_batch(a)

    if a.input is None or a.outdir is None:
        ap.error("input and outdir are required unless --batch is given")

    out_path = a.outdir / a.input.name
    if out_path.resolve() == a.input.resolve():
        raise SystemExit("refusing to overwrite the input in place")

    if not a.verify_only:
        if not a.signals.exists() and not a.append_roundabouts:
            raise SystemExit(f"signals file not found: {a.signals}")
        roundmap = {}
        if not a.no_roundabouts:
            pbf = a.pbf
            if pbf is None and a.pbf_dir is not None:
                abbr = a.input.name.split(".")[0].upper()
                pbf = find_pbf(a.pbf_dir, abbr, _state_table().get(abbr, abbr))
            if pbf is None:
                raise SystemExit(
                    "roundabouts need a PBF: pass --pbf/--pbf-dir, or "
                    "--no-roundabouts to skip type 4")
            print(f"reading roundabouts from {pbf}")
            roundmap, rstat = extract_roundabouts(pbf, a.roundabout_grouping)
            print(f"  {rstat['ways']} roundabout ways -> "
                  f"{rstat['roundabouts']} roundabouts in {len(roundmap)} cells "
                  f"({rstat['dropped']} dropped)")
        if a.append_roundabouts:
            print(f"appending roundabouts {a.input} -> {out_path}")
            st = append_roundabouts(a.input, out_path, roundmap, level=a.level,
                                    allow_existing_type4=a.allow_existing_type4)
            print(f"  {st['existing_entries']} existing entries kept, "
                  f"{st['appended']} roundabouts appended")
        else:
            print(f"loading {a.signals}")
            cellmap = load_intersections(a.signals, None)
            verb = "rebuilding tables in" if a.rebuild_tables else "upgrading"
            print(f"{verb} {a.input} -> {out_path}")
            st = upgrade(a.input, out_path, cellmap, roundmap, level=a.level,
                         rebuild_from_v2=a.rebuild_tables)
            if a.rebuild_tables:
                print(f"  {st['old_entries']} existing entries discarded "
                      f"({_fmt_types(st, prefix='old_')})")
        print(f"  {st['blocks']} blocks, {st['roads']} roads, "
              f"{st['intersections']} intersections ({_fmt_types(st)})")
        print(f"  {st['blocks_with_table']} blocks with intersections, "
              f"{st['blocks_empty_table']} with an empty table")
        if st.get("roundabouts_no_block"):
            print(f"  {st['roundabouts_no_block']} roundabouts dropped: no "
                  "roads block exists for their cell")
        print(f"  {st['in_bytes']:,} -> {st['out_bytes']:,} bytes "
              f"({st['out_bytes'] / st['in_bytes'] - 1:+.1%}) in {st['seconds']}s")

    print(f"verifying {out_path}")
    if a.append_roundabouts:
        problems = verify_append(a.input, out_path, sample=a.sample)
    elif a.rebuild_tables:
        problems = verify_rebuild(a.input, out_path, sample=a.sample)
    else:
        problems = verify(a.input, out_path, sample=a.sample)
    if problems:
        print(f"FAILED: {len(problems)} problem(s)")
        for p in problems[:20]:
            print("  -", p)
        return 1
    print("OK")
    return 0


def _main_batch(a) -> int:
    """Upgrade many states in one process.

    The point of the batch mode is that US.signals is decompressed once for the
    whole run rather than once per state. Roundabout data stays per state --
    each state has its own PBF and there is nothing to share.

    A state that fails is recorded and the run continues; a partial run with an
    explicit failure list beats an aborted one.
    """
    if a.indir is None or a.outdir is None:
        raise SystemExit("--batch needs --indir and --outdir")
    table = _state_table()
    abbrs = ([s.strip().upper() for s in a.states.split(",") if s.strip()]
             if a.states else list(table))
    unknown = [s for s in abbrs if s not in table]
    if unknown:
        raise SystemExit(f"unknown state abbreviations: {unknown}")

    if a.skip_existing:
        keep = [s for s in abbrs if not (a.outdir / f"{s}.roads.ptiles").exists()]
        if len(keep) != len(abbrs):
            print(f"skipping {len(abbrs) - len(keep)} state(s) that already "
                  f"have an output file")
        abbrs = keep
        if not abbrs:
            print("nothing to do")
            return 0

    # Fail loudly BEFORE doing any work if an input or a PBF is missing, so a
    # 51-state run does not die an hour in on a typo.
    missing = []
    inputs = {}
    bad_version = []
    want_version = 2 if (a.append_roundabouts or a.rebuild_tables) else 1
    for s in abbrs:
        p = a.indir / f"{s}.roads.ptiles"
        if not p.exists():
            missing.append(f"input {p}")
        else:
            # Version byte at offset 8. Checked up front so a whole-run report
            # names every unusable input at the start rather than one at a
            # time, hours in. NOT fatal: these are recorded and skipped.
            with open(p, "rb") as fh:
                ver = fh.read(9)[8]
            if ver != want_version:
                bad_version.append((s, ver))
        inputs[s] = p
    pbfs = {}
    if not a.no_roundabouts:
        if a.pbf_dir is None:
            raise SystemExit("--batch needs --pbf-dir (or --no-roundabouts)")
        for s in abbrs:
            try:
                pbfs[s] = find_pbf(a.pbf_dir, s, table[s])
            except FileNotFoundError as e:
                missing.append(str(e))
    if missing:
        print(f"{len(missing)} missing input(s):")
        for m in missing:
            print("  -", m)
        raise SystemExit("refusing to start with missing inputs")

    skipped: list[tuple[str, str]] = []
    if bad_version:
        what = ("this run appends roundabouts to v2 files"
                if a.append_roundabouts
                else "this run rebuilds the tables of v2 files"
                if a.rebuild_tables
                else "this script only upgrades v1 to v2")
        print(f"{len(bad_version)} input(s) are not version {want_version} -- "
              f"{what}, so they are skipped, not rewritten:")
        for s, ver in bad_version:
            print(f"  - {s}: version {ver}")
            skipped.append((s, f"input is version {ver}, not {want_version}"))
        drop = {s for s, _ in bad_version}
        abbrs = [s for s in abbrs if s not in drop]

    t0 = time.time()
    cellmap: dict[int, list[tuple]] = {}
    if not a.append_roundabouts:
        print(f"loading {a.signals} once for {len(abbrs)} states", flush=True)
        ts = time.time()
        cellmap = load_intersections(a.signals, None)
        print(f"  {len(cellmap)} cells, "
              f"{sum(len(v) for v in cellmap.values())} nodes in "
              f"{time.time() - ts:.1f}s", flush=True)

    a.outdir.mkdir(parents=True, exist_ok=True)
    results, failures = [], []
    for i, s in enumerate(abbrs, 1):
        out_path = a.outdir / f"{s}.roads.ptiles"
        if out_path.resolve() == inputs[s].resolve():
            raise SystemExit("refusing to overwrite the input in place")
        t1 = time.time()
        print(f"\n[{i}/{len(abbrs)}] {s}", flush=True)
        try:
            roundmap, rstat = ({}, {"ways": 0, "roundabouts": 0, "dropped": 0})
            if not a.no_roundabouts:
                roundmap, rstat = extract_roundabouts(pbfs[s],
                                                      a.roundabout_grouping)
                print(f"  roundabouts: {rstat['ways']} ways -> "
                      f"{rstat['roundabouts']} in {len(roundmap)} cells",
                      flush=True)
            if a.append_roundabouts:
                st = append_roundabouts(
                    inputs[s], out_path, roundmap, level=a.level,
                    allow_existing_type4=a.allow_existing_type4)
                problems = verify_append(inputs[s], out_path, sample=a.sample)
            else:
                st = upgrade(inputs[s], out_path, cellmap, roundmap,
                             level=a.level, rebuild_from_v2=a.rebuild_tables)
                problems = (verify_rebuild(inputs[s], out_path, sample=a.sample)
                            if a.rebuild_tables
                            else verify(inputs[s], out_path, sample=a.sample))
                if a.rebuild_tables:
                    print(f"  {st['old_entries']} existing entries discarded "
                          f"({_fmt_types(st, prefix='old_')})", flush=True)
            st["state"] = s
            st["elapsed"] = round(time.time() - t1, 1)
            st["roundabout_ways"] = rstat["ways"]
            print(f"  {st['in_bytes']:,} -> {st['out_bytes']:,} bytes "
                  f"({st['out_bytes'] / st['in_bytes'] - 1:+.1%}), "
                  f"{st['blocks']} blocks, {st['roads']} roads", flush=True)
            print(f"  {st['intersections']} intersections ({_fmt_types(st)}) "
                  f"in {st['elapsed']}s", flush=True)
            if st.get("roundabouts_no_block"):
                print(f"  {st['roundabouts_no_block']} roundabouts had no roads "
                      "block for their cell", flush=True)
            if problems:
                st["problems"] = problems
                failures.append((s, f"{len(problems)} verification problem(s): "
                                    + "; ".join(problems[:3])))
                print(f"  VERIFY FAILED: {len(problems)} problem(s)", flush=True)
                for p in problems[:5]:
                    print("    -", p, flush=True)
            else:
                print("  OK", flush=True)
            results.append(st)
        # SystemExit is deliberately caught too: upgrade() reports every
        # refusal that way (already v2, bad magic, unexpected index stride),
        # and SystemExit is a BaseException, so catching only Exception let one
        # unusable input kill a 51-state run partway through.
        except (Exception, SystemExit) as e:         # keep going, record it
            if not isinstance(e, SystemExit):
                import traceback
                traceback.print_exc()
            msg = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
            failures.append((s, msg))
            print(f"  FAILED: {msg}", flush=True)

    # ---------------------------------------------------------- summary
    total = time.time() - t0
    print("\n" + "=" * 78)
    print(f"{'ST':3} {'in MB':>9} {'out MB':>9} {'delta':>7} {'blocks':>7} "
          f"{'roads':>9} {'sig':>7} {'stop':>6} {'give':>5} {'rnd':>5} {'s':>6}")
    print("-" * 78)
    agg = collections.Counter()
    for st in results:
        agg.update({k: v for k, v in st.items() if isinstance(v, int)})
        print(f"{st['state']:3} {st['in_bytes']/1e6:9.1f} {st['out_bytes']/1e6:9.1f} "
              f"{st['out_bytes']/st['in_bytes']-1:+6.1%} {st['blocks']:7} "
              f"{st['roads']:9} {st.get('type_1',0):7} {st.get('type_2',0):6} "
              f"{st.get('type_3',0):5} {st.get('type_4',0):5} {st['elapsed']:6.1f}")
    print("-" * 78)
    print(f"{len(results)} ok, {len(failures)} failed; "
          f"{agg['in_bytes']/1e9:.2f} -> {agg['out_bytes']/1e9:.2f} GB "
          f"({agg['out_bytes']/max(agg['in_bytes'],1)-1:+.1%}); "
          f"{agg['blocks']} blocks, {agg['roads']} roads, "
          f"{agg['intersections']} intersections")
    print(f"  by type: {_fmt_types(agg)}")
    print(f"  wall time {total/60:.1f} min")
    if skipped:
        print(f"\nSKIPPED ({len(skipped)}) -- not upgradable by this script:")
        for s, msg in skipped:
            print(f"  {s}: {msg}")
    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for s, msg in failures:
            print(f"  {s}: {msg}")
        return 1
    return 0 if not skipped else 2


if __name__ == "__main__":
    sys.exit(main())
