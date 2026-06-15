#!/usr/bin/env python3
"""Build missing .highways.ptiles for ME and ND."""

import sys
import struct
import time

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

from pathlib import Path
from collections import defaultdict

import osmium
import h3
import zstandard as zstd

from shared import (
    encode_varint,
    zigzag_encode,
    encode_coordinates,
    write_header,
    write_index,
    HEADER_SIZE,
)
from states import get_state

OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
PBF_DIR = Path("/mnt/aoi/kino/ptiles/pbfs")
MAGIC = b"PTILESR\x00"
VERSION = 2
H3_RES = 7

PBF_MAP = {"ME": "maine", "ND": "north-dakota"}
HIGHWAY_TYPES = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
}

ROAD_CLASS_INDEX = {
    "motorway": 0,
    "motorway_link": 1,
    "trunk": 2,
    "trunk_link": 3,
    "primary": 4,
    "primary_link": 5,
    "secondary": 6,
    "secondary_link": 7,
    "tertiary": 8,
    "tertiary_link": 9,
    "residential": 10,
    "service": 11,
    "unclassified": 12,
    "living_street": 13,
    "track": 14,
    "footway": 15,
    "cycleway": 16,
    "path": 17,
    "bridleway": 18,
    "steps": 19,
    "pedestrian": 20,
    "unknown": 21,
    "rest_area": 22,
    "services": 23,
    "bus_guideway": 24,
    "escape": 25,
    "raceway": 26,
    "busway": 27,
}
ROAD_CLASS_REVERSE = {v: k for k, v in ROAD_CLASS_INDEX.items()}


class HighwayHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.ways = []
        self.count = 0

    def way(self, w):
        hw = None
        for t in w.tags:
            if t.k == "highway" and t.v and t.v in HIGHWAY_TYPES:
                hw = t.v
                break
        if not hw:
            return
        coords = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
        if len(coords) < 2:
            return
        self.ways.append(
            {
                "osm_id": w.id,
                "highway": hw,
                "coords": coords,
                "name": w.tags.get("name"),
                "maxspeed": w.tags.get("maxspeed"),
                "oneway": w.tags.get("oneway"),
                "ref": w.tags.get("ref"),
                "bridge": w.tags.get("bridge"),
            }
        )
        self.count += 1
        if self.count % 5000 == 0:
            print(f"  {self.count} ways...", flush=True)


def split_way_at_cells(way, cell_cache):
    coords = way["coords"]
    segments = []
    cur_cell = None
    cur_seg = []
    for i, (lon, lat) in enumerate(coords):
        try:
            cell = h3.latlng_to_cell(lat, lon, H3_RES)
        except:
            continue
        cell = int(cell, 16) if isinstance(cell, str) else cell
        if cur_cell is not None and cell != cur_cell and len(cur_seg) >= 2:
            segments.append({"cell": cur_cell, "coords": cur_seg[:]})
            cur_seg = [cur_seg[-1], (lon, lat)]
        else:
            cur_seg.append((lon, lat))
        cur_cell = cell
    if len(cur_seg) >= 2:
        segments.append({"cell": cur_cell, "coords": cur_seg})
    return segments


def encode_road_segment(seg, prev_osm_id):
    buf = bytearray()
    delta = seg["osm_id"] - prev_osm_id
    buf.extend(encode_varint(zigzag_encode(delta)))
    coords = seg["coords"]
    n = len(coords)
    buf.append(n if n < 256 else 255)
    if n >= 256:
        buf.extend(struct.pack("<H", n))
    pack1 = struct.pack(
        "<ii", round(coords[0][0] * 100000), round(coords[0][1] * 100000)
    )
    buf.extend(pack1)
    coord_result = encode_coordinates(coords[1:])
    buf.extend(coord_result[0])
    hw_idx = ROAD_CLASS_INDEX.get(seg["highway"], 21)
    buf.append(hw_idx)
    flags = 0
    if seg.get("name"):
        flags |= 0x01
    if seg.get("maxspeed"):
        flags |= 0x02
    if seg.get("oneway") == "yes":
        flags |= 0x08
    if seg.get("oneway") == "-1":
        flags |= 0x10
    if seg.get("bridge") == "yes":
        flags |= 0x20
    if seg.get("ref"):
        flags |= 0x40
    buf.append(flags)
    if seg.get("name"):
        nb = seg["name"].encode("utf-8")
        buf.extend(struct.pack("<H", len(nb)))
        buf.extend(nb)
    if seg.get("maxspeed"):
        ms = seg["maxspeed"].encode("utf-8")
        buf.extend(struct.pack("<B", len(ms)))
        buf.extend(ms)
    if seg.get("ref"):
        rb = seg["ref"].encode("utf-8")
        buf.extend(struct.pack("<B", len(rb)))
        buf.extend(rb)
    return bytes(buf)


def build(abbr):
    pbfn = PBF_MAP.get(abbr)
    if not pbfn:
        return {"abbr": abbr, "error": "no mapping"}
    pbfp = PBF_DIR / f"{pbfn}-latest.osm.pbf"
    if not pbfp.exists():
        return {"abbr": abbr, "error": "no pbf"}
    s = get_state(abbr)
    t0 = time.time()

    h = HighwayHandler()
    h.apply_file(str(pbfp), locations=True)
    print(f"  Extracted {len(h.ways)} highway ways", flush=True)

    segments = []
    for w in h.ways:
        segs = split_way_at_cells(w, {})
        for seg in segs:
            seg["osm_id"] = w["osm_id"]
            seg["highway"] = w["highway"]
            seg["name"] = w.get("name")
            seg["maxspeed"] = w.get("maxspeed")
            seg["oneway"] = w.get("oneway")
            seg["ref"] = w.get("ref")
            seg["bridge"] = w.get("bridge")
        segments.extend(segs)
    print(f"  Split into {len(segments)} cell segments", flush=True)

    if not segments:
        return {"abbr": abbr, "segments": 0}
    per_cell = defaultdict(list)
    for seg in segments:
        per_cell[seg["cell"]].append(seg)
    for c in per_cell:
        per_cell[c].sort(key=lambda s: s["osm_id"])

    sc = sorted(per_cell.keys())
    index_entries = []
    block_offsets = []

    for cell_hex in sc:
        segs = per_cell[cell_hex]
        cell_int = cell_hex
        block_buf = bytearray()
        prev_osm_id = 0
        for seg in segs:
            rec = encode_road_segment(seg, prev_osm_id)
            prev_osm_id = seg["osm_id"]
            block_buf.extend(struct.pack("<I", len(rec)))
            block_buf.extend(rec)
        block_offsets.append((cell_int, bytes(block_buf)))

    # Train dict
    samples = [b for _, b in block_offsets[:2000]]
    dict_data = (
        zstd.train_dictionary(512 * 1024, samples).as_bytes()
        if len(samples) >= 2
        else b""
    )

    # Compress
    zd = zstd.ZstdCompressionDict(dict_data) if dict_data else None
    compressed_blocks = []
    for cell_int, raw in block_offsets:
        if zd:
            cb = zstd.ZstdCompressor(level=12, dict_data=zd).compress(raw)
        else:
            cb = zstd.ZstdCompressor(level=1).compress(raw)
        compressed_blocks.append(cb)

    out = OUTPUT_DIR / f"{abbr}.highways.ptiles"
    with open(out, "wb") as f:
        f.write(b"\x00" * HEADER_SIZE)
        dict_offset = HEADER_SIZE
        dict_length = len(dict_data)
        index_offset = dict_offset + dict_length
        index_length = 4 + len(sc) * 19
        blocks_offset = index_offset + index_length
        cur_off = blocks_offset
        for cell_int, cb in zip([c for c, _ in block_offsets], compressed_blocks):
            index_entries.append(
                {
                    "h3_cell": cell_int,
                    "block_offset": cur_off,
                    "block_length": len(cb),
                    "feature_count": len(per_cell[cell_int]),
                }
            )
            cur_off += len(cb)
        total = sum(e["feature_count"] for e in index_entries)
        f.seek(0)
        write_header(
            f,
            MAGIC,
            VERSION,
            s.min_lat,
            s.min_lon,
            s.max_lat,
            s.max_lon,
            total,
            len(sc),
            dict_offset,
            dict_length,
            index_offset,
            index_length,
            blocks_offset,
        )
        f.write(dict_data)
        write_index(f, index_entries)
        for cb in compressed_blocks:
            f.write(cb)

    dt = time.time() - t0
    sz = out.stat().st_size
    return {"abbr": abbr, "segments": total, "bytes": sz, "time_s": round(dt, 1)}


if __name__ == "__main__":
    for abbr in ["ME", "ND"]:
        try:
            r = build(abbr)
            if r:
                print(
                    f"  {r['abbr']:2s} {r.get('segments', 0):6d} segs  {r.get('bytes', 0):10,d} B  {r.get('time_s', 0):6.1f}s",
                    flush=True,
                )
        except Exception as e:
            print(f"  ERROR {abbr}: {e}", flush=True)
            import traceback

            traceback.print_exc()
