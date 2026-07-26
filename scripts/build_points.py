#!/usr/bin/env python3
"""
Build point-layer .ptiles files (signals, camera) from OSM PBF extracts.

Replaces build_signals.py / build_camera.py / build_all_signals.py /
build_all_camera.py. Those were per-state only; the published files are
national (US.signals.ptiles, US.camera.ptiles), so nothing regenerated what
was actually being served.

  build_points.py                          # national, both layers -> US.*
  build_points.py --layer signals          # national, one layer
  build_points.py --states TN              # single state -> TN.*
  build_points.py --states TN,KY --layer camera
  build_points.py --verify tiles/US.signals.ptiles   # check an existing file

Formats (unchanged, and matched by ptiles-client core/src/{signals,camera}.rs):

  signals  PTILESS v1  osm_id zigzag-delta varint, lon/i32, lat/i32,
                       signal_type/u8, flags/u8, [direction/u16 if 0x01]
  camera   PTILESC v1  osm_id zigzag-delta varint, lon/i32, lat/i32,
                       device_type/u8, placement/u8, camera_type/u8, flags/u8,
                       [direction/u16 0x01] [operator/u8str 0x02]
                       [name/u16str 0x04] [ref/u8str 0x08] [angle/u8 0x10]

Every .ptiles kind is versioned independently -- the version byte is scoped to
the magic and there is no release-wide version. Both of these are v1 and stay
v1 until their own record layout changes.

Index correctness (this is what was broken in the published files):
`index_length` MUST equal 4 + count * INDEX_ENTRY_SIZE_V2 for the count
actually written, and `blocks_offset` MUST equal index_offset + index_length.
The published US.signals/US.camera had index_length computed at a 42-byte
stride while the encoder emits 38, so blocks_offset and every block_offset
overshot by count*4 bytes (432,692 and 145,580) and no block was reachable.
Here the header is derived from the final entry list and then verified by
reading the file back; see verify_file().
"""

import sys

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

import argparse
import struct
import time
from collections import defaultdict
from pathlib import Path

import h3
import zstandard as zstd

from shared import (
    HEADER_SIZE,
    INDEX_ENTRY_SIZE_V2,
    encode_index_entry_v2,
    encode_merged_block,
    encode_varint,
    read_header,
    write_header,
    zigzag_encode,
)
from states import STATES, get_state

OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/tiles")
PBF_DIR = Path("/mnt/aoi/kino/ptiles/pbfs")
H3_RES = 7
CELLS_PER_BLOCK = 8
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

PBF_MAP = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut",
    "DE": "delaware", "DC": "district-of-columbia", "FL": "florida",
    "GA": "georgia", "HI": "hawaii", "ID": "idaho", "IL": "illinois",
    "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky",
    "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana",
    "NE": "nebraska", "NV": "nevada", "NH": "new-hampshire",
    "NJ": "new-jersey", "NM": "new-mexico", "NY": "new-york",
    "NC": "north-carolina", "ND": "north-dakota", "OH": "ohio",
    "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode-island", "SC": "south-carolina", "SD": "south-dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
    "VA": "virginia", "WA": "washington", "WV": "west-virginia",
    "WI": "wisconsin", "WY": "wyoming",
}

SIGNAL_TYPES = ["traffic_signals", "crossing_signals", "stop", "give_way",
                "railway_signals"]
SIGNAL_TYPES_IDX = {t: i for i, t in enumerate(SIGNAL_TYPES)}

DEVICE_TYPES = ["camera", "ALPR", "guard", "unknown"]
DEVICE_TYPES_IDX = {t: i for i, t in enumerate(DEVICE_TYPES)}
PLACEMENTS = ["public", "outdoor", "indoor", "unknown"]
PLACEMENTS_IDX = {t: i for i, t in enumerate(PLACEMENTS)}
CAMERA_TYPES = ["fixed", "panning", "dome", "unknown"]
CAMERA_TYPES_IDX = {t: i for i, t in enumerate(CAMERA_TYPES)}


# --------------------------------------------------------------- extraction

def parse_signal(obj, tags):
    """OSM node -> signal dict, or None if it isn't one."""
    st = None
    direction = None
    for t in tags:
        if t.k == "highway" and t.v in ("traffic_signals", "stop", "give_way"):
            st = t.v
        elif t.k == "crossing" and t.v == "traffic_signals":
            st = "crossing_signals"
        elif t.k in ("traffic_signals:direction", "direction"):
            try:
                direction = max(0, min(359, int(float(t.v))))
            except (ValueError, TypeError):
                pass
    if not st:
        return None
    return {"signal_type": SIGNAL_TYPES_IDX.get(st, 0), "direction": direction}


def parse_camera(obj, tags):
    """OSM node -> camera dict, or None if it isn't one."""
    if not any(t.k == "man_made" and t.v == "surveillance" for t in tags):
        return None
    d = {"device_type": "unknown", "placement": "unknown",
         "camera_type": "unknown", "direction": None, "angle": None,
         "operator": None, "name": None, "ref": None}
    for t in tags:
        if t.k == "surveillance:type" and t.v:
            dt = t.v.lower()
            if dt in DEVICE_TYPES_IDX:
                d["device_type"] = dt
            elif dt in ("alpr", "anpr"):
                d["device_type"] = "ALPR"
        elif t.k == "surveillance" and t.v and t.v.lower() in PLACEMENTS_IDX:
            d["placement"] = t.v.lower()
        elif t.k == "camera:type" and t.v and t.v.lower() in CAMERA_TYPES_IDX:
            d["camera_type"] = t.v.lower()
        elif t.k in ("camera:direction", "direction"):
            try:
                d["direction"] = max(0, min(359, int(float(t.v))))
            except (ValueError, TypeError):
                pass
        elif t.k == "camera:angle" and t.v:
            try:
                d["angle"] = max(0, min(180, int(float(t.v))))
            except (ValueError, TypeError):
                pass
        elif t.k == "operator" and t.v:
            d["operator"] = t.v
        elif t.k == "name" and t.v:
            d["name"] = t.v
        elif t.k == "ref" and t.v:
            d["ref"] = t.v
    return {
        "device_type": DEVICE_TYPES_IDX.get(d["device_type"], 3),
        "placement": PLACEMENTS_IDX.get(d["placement"], 3),
        "camera_type": CAMERA_TYPES_IDX.get(d["camera_type"], 3),
        "direction": d["direction"], "angle": d["angle"],
        "operator": d["operator"], "name": d["name"], "ref": d["ref"],
    }


def enc_signal(p, prev_id):
    b = bytearray()
    b.extend(encode_varint(zigzag_encode(p["osm_id"] - prev_id)))
    b.extend(struct.pack("<ii", round(p["lon"] * 100000),
                         round(p["lat"] * 100000)))
    b.append(p["signal_type"])
    flags = 0x01 if p["direction"] is not None else 0
    b.append(flags)
    if p["direction"] is not None:
        b.extend(struct.pack("<H", p["direction"]))
    return bytes(b)


def enc_camera(p, prev_id):
    b = bytearray()
    b.extend(encode_varint(zigzag_encode(p["osm_id"] - prev_id)))
    b.extend(struct.pack("<ii", round(p["lon"] * 100000),
                         round(p["lat"] * 100000)))
    b.append(p["device_type"])
    b.append(p["placement"])
    b.append(p["camera_type"])
    flags = 0
    if p["direction"] is not None:
        flags |= 0x01
    if p["operator"]:
        flags |= 0x02
    if p["name"]:
        flags |= 0x04
    if p["ref"]:
        flags |= 0x08
    if p["angle"] is not None:
        flags |= 0x10
    b.append(flags)
    if p["direction"] is not None:
        b.extend(struct.pack("<H", p["direction"]))
    if p["operator"]:
        nb = p["operator"].encode("utf-8")[:255]
        b.append(len(nb))
        b.extend(nb)
    if p["name"]:
        nb = p["name"].encode("utf-8")[:65535]
        b.extend(struct.pack("<H", len(nb)))
        b.extend(nb)
    if p["ref"]:
        nb = p["ref"].encode("utf-8")[:255]
        b.append(len(nb))
        b.extend(nb)
    if p["angle"] is not None:
        b.append(p["angle"])
    return bytes(b)


LAYERS = {
    "signals": {"magic": b"PTILESS\x00", "version": 1,
                "keys": ("highway", "crossing"),
                "parse": parse_signal, "enc": enc_signal},
    "camera": {"magic": b"PTILESC\x00", "version": 1,
               "keys": ("man_made",),
               "parse": parse_camera, "enc": enc_camera},
}


def extract(pbf, layers):
    """One pass over a PBF, extracting every requested layer.

    Nodes only. Surveillance mapped as a way (a building outline, say) needs a
    location cache to get a centroid, which costs more than it is worth over
    50 national extracts; those are counted and reported, not silently
    dropped.
    """
    import osmium

    keys = sorted({k for name in layers for k in LAYERS[name]["keys"]})
    fp = osmium.FileProcessor(pbf).with_filter(osmium.filter.KeyFilter(*keys))
    out = {name: [] for name in layers}
    skipped_ways = 0
    for obj in fp:
        lon = getattr(obj, "lon", None)
        if lon is None:
            # A way/relation matched the key filter; we only handle nodes.
            skipped_ways += 1
            continue
        tags = obj.tags
        for name in layers:
            rec = LAYERS[name]["parse"](obj, tags)
            if rec is None:
                continue
            # Quantize to the microdegrees the record will actually store,
            # THEN pick the cell. Indexing at full precision and storing
            # rounded coords lets a point within ~1e-5 deg of a cell edge
            # round across it, so the file indexes it under a cell its own
            # payload says it isn't in. verify_file() checks for exactly this.
            lon_q = round(lon * 100000) / 100000
            lat_q = round(obj.lat * 100000) / 100000
            try:
                c = h3.latlng_to_cell(lat_q, lon_q, H3_RES)
            except Exception:
                continue
            rec.update({"osm_id": obj.id, "lon": lon_q, "lat": lat_q,
                        "cell": int(c, 16) if isinstance(c, str) else c})
            out[name].append(rec)
    return out, skipped_ways


# ------------------------------------------------------------------ writing

def train_dict(blocks):
    """Train a zstd dictionary, shrinking the target until it fits the sample.

    Sparse single-state layers (DC cameras, say) don't have enough block data
    to train a 512 KiB dictionary and zstd errors out rather than degrading.
    An empty dictionary is a valid file: dict_length 0, blocks compressed
    without one, and readers already branch on dict_length > 0.
    """
    for size in (512 * 1024, 110 * 1024, 16 * 1024, 4 * 1024):
        try:
            return zstd.train_dictionary(size, blocks[:2000]).as_bytes()
        except zstd.ZstdError:
            continue
    print("    (not enough block data to train a dictionary; "
          "writing dict_length=0)")
    return b""


def write_layer(name, points, out_stem, bbox):
    """Write one .ptiles file. Returns a stats dict."""
    spec = LAYERS[name]
    by_cell = defaultdict(list)
    for p in points:
        by_cell[p["cell"]].append(p)
    for c in by_cell:
        by_cell[c].sort(key=lambda p: p["osm_id"])

    sorted_cells = sorted(by_cell)
    blocks = []
    entries = []
    for block_idx, i in enumerate(range(0, len(sorted_cells), CELLS_PER_BLOCK)):
        chunk = sorted_cells[i:i + CELLS_PER_BLOCK]
        cell_records = []
        for cell in chunk:
            recs = []
            prev_id = 0
            for p in by_cell[cell]:
                recs.append(spec["enc"](p, prev_id))
                prev_id = p["osm_id"]
            cell_records.append((cell, recs))
        clat, clon = h3.cell_to_latlng(hex(chunk[0])[2:])
        blocks.append(encode_merged_block(cell_records,
                                          round(clon * 100000),
                                          round(clat * 100000)))
        for pos, cell in enumerate(chunk):
            pts = by_cell[cell]
            entries.append({
                "h3_cell": cell,
                # Carried explicitly so sorting entries can never decouple an
                # entry from the block it points at.
                "block_idx": block_idx,
                "feature_count": len(pts),
                "cell_index": pos,
                "min_lon": round(min(p["lon"] for p in pts) * 100000),
                "min_lat": round(min(p["lat"] for p in pts) * 100000),
                "max_lon": round(max(p["lon"] for p in pts) * 100000),
                "max_lat": round(max(p["lat"] for p in pts) * 100000),
            })

    dict_bytes = train_dict(blocks)
    cctx = (zstd.ZstdCompressor(level=12,
                                dict_data=zstd.ZstdCompressionDict(dict_bytes))
            if dict_bytes else zstd.ZstdCompressor(level=12))
    comp = [cctx.compress(b) for b in blocks]

    # Cumulative block starts, so this is linear rather than the quadratic
    # sum(comp[:idx]) the old scripts did per entry.
    starts = [0]
    for cb in comp:
        starts.append(starts[-1] + len(cb))

    entries.sort(key=lambda e: e["h3_cell"])

    # Every offset below is derived from `entries` as finally written. The
    # published files broke precisely by computing these from something else.
    dict_offset = HEADER_SIZE
    dict_length = len(dict_bytes)
    index_offset = dict_offset + dict_length
    index_length = 4 + len(entries) * INDEX_ENTRY_SIZE_V2
    blocks_offset = index_offset + index_length

    out_path = OUTPUT_DIR / f"{out_stem}.{name}.ptiles"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        write_header(f, spec["magic"], spec["version"],
                     bbox[0], bbox[1], bbox[2], bbox[3],
                     sum(e["feature_count"] for e in entries), len(blocks),
                     dict_offset, dict_length, index_offset, index_length,
                     blocks_offset)
        f.write(dict_bytes)
        f.write(struct.pack("<I", len(entries)))
        for e in entries:
            f.write(encode_index_entry_v2(
                e["h3_cell"], e["min_lon"], e["min_lat"], e["max_lon"],
                e["max_lat"], blocks_offset + starts[e["block_idx"]],
                len(comp[e["block_idx"]]), e["feature_count"],
                e["cell_index"]))
        for cb in comp:
            f.write(cb)

    return {"layer": name, "path": out_path, "points": len(points),
            "cells": len(by_cell), "blocks": len(blocks),
            "bytes": out_path.stat().st_size}


# ---------------------------------------------------------------- verifying

def layer_of(magic):
    for name, spec in LAYERS.items():
        if spec["magic"].rstrip(b"\x00") == magic.rstrip(b"\x00"):
            return name
    raise ValueError(f"unknown magic {magic!r}")


def decode_records(name, body):
    """Mirror of ptiles-client core/src/{signals,camera}.rs.

    Back-to-back records, no length prefix. Returns (osm_id, lon, lat) tuples.
    Raises if the stream doesn't consume exactly, which is how a field-width
    or flag-bit disagreement with the encoder shows up.
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
        prev += (val >> 1) ^ -(val & 1)
        lon, lat = struct.unpack_from("<ii", body, p)
        p += 8
        if name == "signals":
            p += 1                       # signal_type
            flags = body[p]
            p += 1
            if flags & 0x01:
                p += 2                   # direction u16
        else:
            p += 3                       # device_type, placement, camera_type
            flags = body[p]
            p += 1
            if flags & 0x01:
                p += 2                   # direction u16
            if flags & 0x02:             # operator, u8-prefixed
                p += 1 + body[p]
            if flags & 0x04:             # name, u16-prefixed
                n = struct.unpack_from("<H", body, p)[0]
                p += 2 + n
            if flags & 0x08:             # ref, u8-prefixed
                p += 1 + body[p]
            if flags & 0x10:
                p += 1                   # angle u8
        out.append((prev, lon / 1e5, lat / 1e5))
    if p != len(body):
        raise ValueError(f"record stream desync: consumed {p} of {len(body)}")
    return out


def verify_file(path, sample=8):
    """Read a written file back. Raises AssertionError on any inconsistency.

    Checks the exact class of bug that shipped: header offsets that disagree
    with the index actually written, and block offsets that don't land on a
    zstd frame.
    """
    path = Path(path)
    problems = []
    with open(path, "rb") as f:
        h = read_header(f)
        f.seek(h["index_offset"])
        count = struct.unpack("<I", f.read(4))[0]

        expect_il = 4 + count * INDEX_ENTRY_SIZE_V2
        if h["index_length"] != expect_il:
            problems.append(
                f"index_length {h['index_length']} != 4 + {count}*"
                f"{INDEX_ENTRY_SIZE_V2} = {expect_il} "
                f"(stride implied: {(h['index_length'] - 4) / count:g})")
        expect_bo = h["index_offset"] + expect_il
        if h["blocks_offset"] != expect_bo:
            problems.append(
                f"blocks_offset {h['blocks_offset']} != {expect_bo} "
                f"(skew {h['blocks_offset'] - expect_bo})")

        raw = f.read(count * INDEX_ENTRY_SIZE_V2)
        entries = []
        for i in range(count):
            e = raw[i * INDEX_ENTRY_SIZE_V2:(i + 1) * INDEX_ENTRY_SIZE_V2]
            cell = struct.unpack_from("<Q", e, 0)[0]
            off = (e[32] << 48) | int.from_bytes(e[24:30], "little")
            ln = (e[33] << 16) | struct.unpack_from("<H", e, 30)[0]
            fc = struct.unpack_from("<H", e, 34)[0]
            entries.append((cell, off, ln, fc))

        cells = [e[0] for e in entries]
        if cells != sorted(cells):
            problems.append("index entries are not sorted by h3_cell")

        f.seek(h["dict_offset"])
        dict_bytes = f.read(h["dict_length"]) if h["dict_length"] else b""
        dctx = (zstd.ZstdDecompressor(
            dict_data=zstd.ZstdCompressionDict(dict_bytes))
            if dict_bytes else zstd.ZstdDecompressor())

        step = max(1, len(entries) // sample)
        checked = decoded = 0
        for cell, off, ln, fc in entries[::step][:sample]:
            f.seek(off)
            blob = f.read(ln)
            if blob[:4] != ZSTD_MAGIC:
                problems.append(
                    f"cell {cell:#x}: offset {off} is not a zstd frame "
                    f"(got {blob[:4].hex()})")
                continue
            block = dctx.decompressobj().decompress(blob)
            _, _, cc = struct.unpack_from("<iiI", block, 0)
            tbl = [struct.unpack_from("<QI", block, 12 + 12 * i)
                   for i in range(cc)]
            if cell not in [c for c, _ in tbl]:
                problems.append(
                    f"cell {cell:#x} absent from the block it points at")
                continue

            # Decode this cell's records with the same layout
            # ptiles-client's core/src/{signals,camera}.rs uses, and confirm
            # every point actually lands in the cell that indexes it. The
            # published files were never checked this way.
            body_start = 12 + 12 * cc
            pos = [c for c, _ in tbl].index(cell)
            lo = body_start + tbl[pos][1]
            hi = (body_start + tbl[pos + 1][1] if pos + 1 < cc
                  else len(block))
            try:
                recs = decode_records(layer_of(h["magic"]), block[lo:hi])
            except Exception as ex:
                problems.append(f"cell {cell:#x}: record decode failed ({ex})")
                continue
            if len(recs) != fc:
                problems.append(
                    f"cell {cell:#x}: decoded {len(recs)} records, "
                    f"index says feature_count={fc}")
            stray = [r for r in recs
                     if int(h3.latlng_to_cell(r[2], r[1], H3_RES), 16) != cell]
            if stray:
                problems.append(
                    f"cell {cell:#x}: {len(stray)}/{len(recs)} records fall "
                    f"outside the cell that indexes them")
            checked += 1
            decoded += len(recs)

        if checked == 0 and entries:
            problems.append("no sampled block could be read")

    if problems:
        raise AssertionError(
            f"{path.name}: " + "; ".join(problems))
    return {"count": count, "blocks_checked": checked,
            "cells_in_blocks": decoded, "magic": h["magic"],
            "version": h["version"]}


# --------------------------------------------------------------------- main

def run(layers, abbrs, out_stem):
    collected = {name: [] for name in layers}
    seen = {name: set() for name in layers}
    lats, lons = [], []
    t0 = time.time()

    for abbr in abbrs:
        pbf_name = PBF_MAP.get(abbr)
        if not pbf_name:
            print(f"  {abbr}: no PBF mapping, skipped")
            continue
        pbf = PBF_DIR / f"{pbf_name}-latest.osm.pbf"
        if not pbf.exists():
            print(f"  {abbr}: {pbf.name} missing, skipped")
            continue
        ts = time.time()
        found, skipped_ways = extract(str(pbf), layers)
        counts = []
        for name, pts in found.items():
            fresh = [p for p in pts if p["osm_id"] not in seen[name]]
            seen[name].update(p["osm_id"] for p in fresh)
            collected[name].extend(fresh)
            counts.append(f"{name}={len(fresh)}"
                          + (f" (+{len(pts) - len(fresh)} dup)"
                             if len(pts) != len(fresh) else ""))
        st = get_state(abbr)
        if st:
            lats += [st.min_lat, st.max_lat]
            lons += [st.min_lon, st.max_lon]
        print(f"  {abbr:2s} {' '.join(counts):40s} "
              f"ways_skipped={skipped_ways:<6d} {time.time() - ts:6.1f}s")

    if not lats:
        raise SystemExit("no states produced a bbox; nothing to write")
    bbox = (min(lats), min(lons), max(lats), max(lons))

    results = []
    for name in layers:
        pts = collected[name]
        if not pts:
            print(f"\n{name}: no points found, not writing a file")
            continue
        r = write_layer(name, pts, out_stem, bbox)
        v = verify_file(r["path"])
        r["verified"] = v
        results.append(r)
        print(f"\n{name}: {r['points']:,} points  {r['cells']:,} cells  "
              f"{r['blocks']:,} blocks  {r['bytes']:,} B")
        print(f"  -> {r['path']}")
        print(f"  verified: {v['magic'].decode()} v{v['version']}, "
              f"{v['count']:,} index entries, "
              f"{v["blocks_checked"]} cells sampled, {v["cells_in_blocks"]:,} records decoded")
    print(f"\nDone in {time.time() - t0:.1f}s")
    return results


def main():
    ap = argparse.ArgumentParser(
        description="Build signals/camera .ptiles (national by default)")
    ap.add_argument("--layer", choices=sorted(LAYERS),
                    help="only this layer (default: all)")
    ap.add_argument("--states",
                    help="comma-separated abbrs for a single-state build; "
                         "omit for a national US.* build")
    ap.add_argument("--verify", metavar="PATH",
                    help="verify an existing .ptiles file and exit")
    args = ap.parse_args()

    if args.verify:
        try:
            v = verify_file(args.verify)
        except AssertionError as e:
            print(f"FAIL {e}")
            return 1
        print(f"OK {args.verify}: {v['magic'].decode()} v{v['version']}, "
              f"{v['count']:,} entries, {v['blocks_checked']} blocks sampled")
        return 0

    layers = [args.layer] if args.layer else sorted(LAYERS)

    if args.states:
        abbrs = []
        for a in args.states.split(","):
            st = get_state(a.strip())
            if st:
                abbrs.append(st.abbr)
            else:
                print(f"Unknown state: {a}")
        if len(abbrs) != 1:
            # Several states still produce one file; name it after the first
            # so it never silently overwrites a national build.
            print(f"note: {len(abbrs)} states -> one "
                  f"{abbrs[0] if abbrs else '?'}.* file")
        stem = abbrs[0] if abbrs else None
    else:
        abbrs = [s.abbr for s in STATES if s.abbr in PBF_MAP]
        stem = "US"

    if not abbrs:
        raise SystemExit("no buildable states")
    print(f"layers={','.join(layers)} states={len(abbrs)} -> {stem}.*")
    run(layers, abbrs, stem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
