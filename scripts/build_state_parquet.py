#!/usr/bin/env python3
"""
Build a unified state Parquet file from existing PTILES files on disk.

Reads each PTILES layer, decodes every record, writes to a single Parquet
file with separate Pandas DataFrames (one per layer).

Usage:
    uv run --with pyarrow --with pandas --with shapely --with h3 --with osmium --with zstandard \
        python build_state_parquet.py TN

Output:
    /home/aoi/kino/projects/ptiles/data/parquet/TN.parquet

Requires PTILES files at:
    /home/aoi/kino/projects/ptiles/data/states/TN.roads.ptiles
    /home/aoi/kino/projects/ptiles/data/states/TN.water.ptiles
    /home/aoi/kino/projects/ptiles/data/states/TN.buildings_v8.ptiles
    /home/aoi/kino/projects/ptiles/data/states/TN.business.ptiles
    /home/aoi/kino/projects/ptiles/data/states/TN.parks.ptiles
    /home/aoi/kino/projects/ptiles/data/states/TN.rail.ptiles
    /home/aoi/kino/projects/ptiles/data/states/TN.places.ptiles
"""

import sys
import os
import struct
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from shared import (
    read_header,
    read_index,
    decompress_block,
    decode_varint,
    zigzag_decode,
)

DATA_DIR = "/home/aoi/kino/projects/ptiles/data/states"
OUT_DIR = "/home/aoi/kino/projects/ptiles/data/parquet"

ROAD_CLASS_NAMES = {
    0: "motorway",
    1: "trunk",
    2: "primary",
    3: "secondary",
    4: "tertiary",
    5: "unclassified",
    6: "residential",
    7: "service",
    8: "living_street",
    9: "track",
    10: "path",
    11: "pedestrian",
    12: "steps",
    13: "construction",
    14: "rest_area",
    15: "services",
}


def decode_roads(path, state):
    """Decode roads PTILES file. Uses the old per-cell v1/merged v2 format."""
    rows = []
    with open(path, "rb") as f:
        hdr = read_header(f)
        d = open(path, "rb").read()
        dict_data = d[hdr["dict_offset"] : hdr["dict_offset"] + hdr["dict_length"]]
        f.seek(hdr["index_offset"])
        idx_data = f.read(hdr["index_length"])
        entries = read_index(idx_data)

    for i, e in enumerate(entries):
        with open(path, "rb") as f:
            off = e["block_offset"]
            f.seek(off)
            comp = f.read(e["block_length"])
        raw = decompress_block(comp, dict_data)

        # Decode records (roads use u32 record_length prefix)
        p = 0
        prev_osm = 0
        while p + 4 <= len(raw):
            rl = struct.unpack_from("<I", raw, p)[0]
            p += 4
            if rl == 0 or p + rl > len(raw):
                break
            rec = raw[p : p + rl]
            p += rl
            try:
                rp = 0
                dr, consumed = decode_varint(rec, rp)
                rp += consumed
                osm_id = prev_osm + dr  # NOT zigzag for roads
                prev_osm = osm_id
                if rp + 2 > len(rec):
                    continue
                vc = struct.unpack_from("<H", rec, rp)[0]
                rp += 2
                if vc < 2 or rp + 8 > len(rec):
                    continue
                first_lon = struct.unpack_from("<i", rec, rp)[0]
                first_lat = struct.unpack_from("<i", rec, rp + 4)[0]
                rp += 8
                lon, lat = first_lon / 100000, first_lat / 100000
                coords = [(lon, lat)]
                for _ in range(vc - 1):
                    dr1, c1 = decode_varint(rec, rp)
                    rp += c1
                    dr2, c2 = decode_varint(rec, rp)
                    rp += c2
                    lon += zigzag_decode(dr1) / 100000
                    lat += zigzag_decode(dr2) / 100000
                    coords.append((lon, lat))
                if rp >= len(rec):
                    rows.append(
                        {
                            "osm_id": osm_id,
                            "highway": None,
                            "name": None,
                            "ref": None,
                            "highway_code": 8,
                            "geometry_wkt": None,
                        }
                    )
                    continue
                # Road class (indexed byte) then flags
                rc = rec[rp]
                rp += 1
                if rp >= len(rec):
                    rows.append(
                        {
                            "osm_id": osm_id,
                            "highway": ROAD_CLASS_NAMES.get(rc, f"class_{rc}"),
                            "name": None,
                            "ref": None,
                            "highway_code": rc,
                            "geometry_wkt": None,
                        }
                    )
                    continue
                flags = rec[rp]
                rp += 1
                name = None
                ref = None
                if flags & 0x01 and rp + 2 <= len(rec):
                    nlen = struct.unpack_from("<H", rec, rp)[0]
                    rp += 2
                    if rp + nlen <= len(rec):
                        name = rec[rp : rp + nlen].decode("utf-8", errors="replace")
                        rp += nlen
                if flags & 0x02 and rp + 2 <= len(rec):
                    rlen = struct.unpack_from("<H", rec, rp)[0]
                    rp += 2
                    if rp + rlen <= len(rec):
                        ref = rec[rp : rp + rlen].decode("utf-8", errors="replace")
                        rp += rlen
                # Build WKT linestring
                if len(coords) >= 2:
                    wkt = (
                        "LINESTRING ("
                        + ", ".join(f"{c[0]} {c[1]}" for c in coords)
                        + ")"
                    )
                else:
                    wkt = None
                rows.append(
                    {
                        "osm_id": osm_id,
                        "highway": ROAD_CLASS_NAMES.get(rc, f"class_{rc}"),
                        "name": name,
                        "ref": ref,
                        "highway_code": rc,
                        "geometry_wkt": wkt,
                    }
                )
            except Exception as e:
                continue

    return pd.DataFrame(rows)


def decode_water(path, state):
    """Decode water PTILES file."""
    rows = []
    with open(path, "rb") as f:
        hdr = read_header(f)
        d = open(path, "rb").read()
        dict_data = d[hdr["dict_offset"] : hdr["dict_offset"] + hdr["dict_length"]]
        f.seek(hdr["index_offset"])
        idx_data = f.read(hdr["index_length"])
        entries = read_index(idx_data)

    for e in entries:
        with open(path, "rb") as f:
            off = e["block_offset"]
            f.seek(off)
            comp = f.read(e["block_length"])
        raw = decompress_block(comp, dict_data)

        p = 0
        prev_osm = 0
        while p < len(raw):
            try:
                dr, consumed = decode_varint(raw, p)
                p += consumed
                osm_id = prev_osm + zigzag_decode(dr)
                prev_osm = osm_id
                if p >= len(raw):
                    break
                geom_type = raw[p]
                p += 1
                name = None
                width = None
                coords = []

                if geom_type == 2:
                    # Reference — skip 4 bytes
                    p += 4
                    continue
                elif geom_type in (0, 1):
                    if p + 2 > len(raw):
                        break
                    vc = struct.unpack_from("<H", raw, p)[0]
                    p += 2
                    if vc < 2 or p + 8 > len(raw):
                        break
                    first_lon = struct.unpack_from("<i", raw, p)[0]
                    first_lat = struct.unpack_from("<i", raw, p + 4)[0]
                    p += 8
                    lon, lat = first_lon / 100000, first_lat / 100000
                    coords = [(lon, lat)]
                    for _ in range(vc - 1):
                        dr1, c1 = decode_varint(raw, p)
                        p += c1
                        dr2, c2 = decode_varint(raw, p)
                        p += c2
                        lon += zigzag_decode(dr1) / 100000
                        lat += zigzag_decode(dr2) / 100000
                        coords.append((lon, lat))
                if p >= len(raw):
                    continue
                flags = raw[p]
                p += 1
                water_type_byte = raw[p] if p < len(raw) else 0
                p += 1
                if flags & 0x01 and p + 2 <= len(raw):
                    nlen = struct.unpack_from("<H", raw, p)[0]
                    p += 2
                    if p + nlen <= len(raw):
                        name = raw[p : p + nlen].decode("utf-8", errors="replace")
                        p += nlen
                if flags & 0x02 and p + 2 <= len(raw):
                    width = struct.unpack_from("<H", raw, p)[0]
                    p += 2

                wkt = None
                if len(coords) >= 2:
                    if geom_type == 0:
                        wkt = (
                            "POLYGON (("
                            + ", ".join(f"{c[0]} {c[1]}" for c in coords)
                            + "))"
                        )
                    else:
                        wkt = (
                            "LINESTRING ("
                            + ", ".join(f"{c[0]} {c[1]}" for c in coords)
                            + ")"
                        )
                rows.append(
                    {
                        "osm_id": osm_id,
                        "water_type": water_type_byte,
                        "name": name,
                        "width": width,
                        "geometry_wkt": wkt,
                    }
                )
            except Exception:
                continue

    return pd.DataFrame(rows)


def main():
    state = sys.argv[1].upper() if len(sys.argv) > 1 else "TN"
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()

    layers = {
        "roads": {"file": f"{state}.roads.ptiles", "fn": decode_roads},
        "water": {"file": f"{state}.water.ptiles", "fn": decode_water},
        # buildings, business, parks, rail, places would go here
        # with their respective decode functions
    }

    tables = {}
    for name, info in layers.items():
        path = os.path.join(DATA_DIR, info["file"])
        if not os.path.exists(path):
            print(f"  SKIP {name}: {path} not found", flush=True)
            continue
        print(f"  Decoding {name}...", flush=True)
        t1 = time.time()
        df = info["fn"](path, state)
        print(f"    {len(df)} rows in {time.time() - t1:.1f}s", flush=True)
        tables[name] = pa.Table.from_pandas(df)

    output_path = os.path.join(OUT_DIR, f"{state}.parquet")
    with pq.ParquetWriter(output_path, tables["roads"].schema) as writer:
        writer.write_table(tables["roads"])

    print(f"\nWrote {output_path} in {time.time() - t0:.1f}s", flush=True)
    sz = os.path.getsize(output_path)
    print(f"  Size: {sz / 1024 / 1024:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
