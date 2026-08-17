#!/usr/bin/env python3
"""
Build a {REGION}.roads.ptiles — three zoom bands in one file, no H3 split.

Each road is encoded once per zoom band. Lower zoom bands use
Douglas-Peucker simplified geometry.

File layout (256-byte header + 3 ZSTD frames):
  header: magic(4) + version(1) + reserved(3)
          + z04_off(8) + z04_comp(4) + z04_decomp(4)
          + z05_off(8) + z05_comp(4) + z05_decomp(4)
          + z07_off(8) + z07_comp(4) + z07_decomp(4)
          + road_count(4)                          @56, == z07_count
          + z04_dict_len(4) + z05_dict_len(4) + z07_dict_len(4)   @60
          + z04_count(4) + z05_count(4) + z07_count(4)            @72
          + pad(172)
  Band dictionaries are concatenated at offset 256 in Z04, Z05, Z07 order;
  split them using the three dict_len fields.
  Z04 frame (res 4, zoom 5-9): highways only, epsilon=500m
  Z05 frame (res 5, zoom 10-12): all roads, epsilon=200m
  Z07 frame (res 7, zoom 13+): all roads, full precision

Road record format (same for all bands):
  varint osm_id_delta
  u16 vertex_count
  i32 first_lon_micro, first_lat_micro
  [varint lon_delta, varint lat_delta] * (vertex_count-1)
  u8 road_class (indexed, 0=unknown)
  u8 flags
  [optional fields per flags]

Usage:
  uv run --with osmium --with h3 --with zstandard --with shapely \
    python build_roads.py <input.pbf> <output.ptiles>
"""

import sys
import os
import struct
import time

import osmium
import zstandard as zstd
from shapely.geometry import LineString

sys.path.insert(0, os.path.dirname(__file__))
from array import array

from shared import encode_varint, encode_coordinates, coord_to_micro, micro_to_coord

# ===========================================================================
# Constants
# ===========================================================================

HEADER_SIZE = 256
FORMAT_VERSION = 2  # v2 adds per-band dict lengths + per-band road counts

# Highway tags absent from ROAD_CLASS_INDEX (highway=road, busway, corridor,
# and anything new OSM adds). Must not default to 0 -- that is motorway, which
# would promote unknown ways into the Z04 highway-only band.
DEFAULT_ROAD_CLASS = 5  # unclassified

ZOOM_BANDS = [
    {
        "name": "z04",
        "zoom_min": 5,
        "zoom_max": 9,
        "simplify_eps": 500,
        "max_road_class": 2,  # motorway/trunk/primary only
        "magic_byte": 4,
    },
    {
        "name": "z05",
        "zoom_min": 10,
        "zoom_max": 12,
        "simplify_eps": 200,
        "max_road_class": 15,  # all classes
        "magic_byte": 5,
    },
    {
        "name": "z07",
        "zoom_min": 13,
        "zoom_max": 20,
        "simplify_eps": 0,
        "max_road_class": 15,
        "magic_byte": 7,
    },
]

ROAD_CLASS_INDEX = {
    "motorway": 0,
    "motorway_link": 0,
    "trunk": 1,
    "trunk_link": 1,
    "primary": 2,
    "primary_link": 2,
    "secondary": 3,
    "secondary_link": 3,
    "tertiary": 4,
    "tertiary_link": 4,
    "unclassified": 5,
    "residential": 6,
    "service": 7,
    "living_street": 8,
    "track": 9,
    "path": 10,
    "footway": 10,
    "cycleway": 10,
    "bridleway": 10,
    "pedestrian": 11,
    "steps": 12,
    "construction": 13,
    "rest_area": 14,
    "services": 15,
}


# ===========================================================================
# PBF extraction
# ===========================================================================


class RoadExtractor(osmium.SimpleHandler):
    """Collect every highway way in the PBF.

    Roads are held as 5-tuples with coordinates in an array('i') of interleaved
    micro-degrees, not dicts of float tuples. A country-sized PBF has tens of
    millions of vertices and a Python (float, float) tuple costs ~80 bytes
    against 8 for two array slots -- Japan OOMs at 14 GB the other way.
    Micro-degrees are lossless here: encode_coordinates rounds to 1e5 anyway.
    """

    def __init__(self):
        super().__init__()
        self.roads = []

    def way(self, w):
        tags = dict(w.tags)
        highway = tags.get("highway")
        if not highway:
            return
        if highway in ("proposed", "construction", "raceway", "escape"):
            return
        coords = array("i")
        try:
            for n in w.nodes:
                coords.append(coord_to_micro(n.lon))
                coords.append(coord_to_micro(n.lat))
        except osmium.InvalidLocationError:
            return
        if len(coords) < 4:  # fewer than 2 vertices
            return
        self.roads.append(
            (w.id, coords, highway, tags.get("name"), tags.get("ref"))
        )


def extract_roads(pbf_path):
    print(f"Extracting roads from {pbf_path}...", flush=True)
    handler = RoadExtractor()
    handler.apply_file(pbf_path, locations=True)
    print(f"  {len(handler.roads):,} roads extracted", flush=True)
    return handler.roads


# ===========================================================================
# Road encoding
# ===========================================================================


def encode_road(osm_id, prev_osm_id, coords, highway, road_class, name, ref):
    """Encode one road as a byte sequence."""
    buf = bytearray()
    buf.extend(encode_varint(osm_id - prev_osm_id))
    buf.extend(struct.pack("<H", len(coords)))
    delta_bytes, first_lon, first_lat = encode_coordinates(coords)
    buf.extend(struct.pack("<ii", first_lon, first_lat))
    buf.extend(delta_bytes)

    # Road class (indexed) -- resolved by the caller, must match band filtering
    buf.append(min(road_class, 255))

    # Flags
    flags = 0
    if name:
        flags |= 0x01
    if ref:
        flags |= 0x02
    buf.append(flags)
    if flags & 0x01:
        n = name.encode("utf-8")
        buf.extend(struct.pack("<H", len(n)))
        buf.extend(n)
    if flags & 0x02:
        r = ref.encode("utf-8")
        buf.extend(struct.pack("<H", len(r)))
        buf.extend(r)
    return bytes(buf)


# ===========================================================================
# Geometry simplification
# ===========================================================================


def simplify_coords(coords, epsilon):
    """Douglas-Peucker simplification. Returns simplified coords list."""
    if epsilon <= 0 or len(coords) < 3:
        return coords
    try:
        line = LineString(coords)
        simplified = line.simplify(epsilon / 111320.0, preserve_topology=False)
        if len(simplified.coords) >= 2:
            return [(c[0], c[1]) for c in simplified.coords]
    except Exception:
        pass
    return coords


# ===========================================================================
# Build one zoom band
# ===========================================================================


def build_zoom_band(roads, epsilon, max_road_class, dict_data, level):
    """Encode all (filtered + simplified) roads for one zoom band."""
    buf = bytearray()
    prev_osm = 0
    road_count = 0
    skipped_class = 0
    skipped_short = 0

    for osm_id, mcoords, highway, name, ref in roads:
        rc = ROAD_CLASS_INDEX.get(highway, DEFAULT_ROAD_CLASS)
        if rc > max_road_class:
            skipped_class += 1
            continue

        # Micro-degrees back to float only for this one road -- see RoadExtractor
        coords = [
            (micro_to_coord(mcoords[i]), micro_to_coord(mcoords[i + 1]))
            for i in range(0, len(mcoords), 2)
        ]
        if epsilon > 0:
            coords = simplify_coords(coords, epsilon)
        if len(coords) < 2:
            skipped_short += 1
            continue

        record = encode_road(osm_id, prev_osm, coords, highway, rc, name, ref)
        buf.extend(record)
        prev_osm = osm_id
        road_count += 1

    # Train dict on sample of this band's data
    samples = [
        bytes(buf[i : i + 1024])
        for i in range(0, min(len(buf), 500 * 1024), 1024)
        if i + 64 < len(buf)
    ]
    band_dict = (
        zstd.train_dictionary(256 * 1024, samples).as_bytes() if samples else b""
    )
    d = zstd.ZstdCompressionDict(band_dict)
    cctx = zstd.ZstdCompressor(level=12, dict_data=d)
    compressed = cctx.compress(bytes(buf))

    print(
        f"  {level}: {road_count:,} roads, "
        f"{len(buf) / 1024 / 1024:.1f} MB decompressed, "
        f"{len(compressed) / 1024 / 1024:.1f} MB compressed "
        f"(skipped {skipped_class} by class, {skipped_short} by short)",
        flush=True,
    )
    return compressed, len(buf), road_count, band_dict


# ===========================================================================
# Write final file
# ===========================================================================


def write_ptiles(
    output_path,
    z04_data,
    z04_decomp,
    z04_count,
    z05_data,
    z05_decomp,
    z05_count,
    z07_data,
    z07_decomp,
    z07_count,
    band_dicts,
):
    """Write 256-byte header + 3 ZSTD frames."""
    # Layout
    dict_offset = HEADER_SIZE
    # Write all three band dicts concatenated after header
    dict_data = b"".join(band_dicts)
    dict_length = len(dict_data)

    z04_off = dict_offset + dict_length
    z04_off = (z04_off + 3) & ~3
    z05_off = z04_off + len(z04_data)
    z07_off = z05_off + len(z05_data)

    # Z07 is the only unfiltered band (max_road_class=15, epsilon=0), so it
    # carries the true road total. Z04 is motorway/trunk/primary only.
    total_roads = z07_count

    with open(output_path, "wb") as f:
        # Header
        hdr = bytearray(HEADER_SIZE)
        hdr[0:4] = b"PTLR"
        hdr[4] = FORMAT_VERSION
        struct.pack_into("<Q", hdr, 8, z04_off)
        struct.pack_into("<I", hdr, 16, len(z04_data))
        struct.pack_into("<I", hdr, 20, z04_decomp)
        struct.pack_into("<Q", hdr, 24, z05_off)
        struct.pack_into("<I", hdr, 32, len(z05_data))
        struct.pack_into("<I", hdr, 36, z05_decomp)
        struct.pack_into("<Q", hdr, 40, z07_off)
        struct.pack_into("<I", hdr, 48, len(z07_data))
        struct.pack_into("<I", hdr, 52, z07_decomp)
        struct.pack_into("<I", hdr, 56, total_roads)
        # Per-band dict lengths -- dicts are concatenated at HEADER_SIZE with no
        # framing of their own, so a reader cannot split them without these.
        struct.pack_into("<III", hdr, 60, *(len(d) for d in band_dicts))
        # Per-band road counts (each band filters differently)
        struct.pack_into("<III", hdr, 72, z04_count, z05_count, z07_count)
        f.write(hdr)

        # Band dictionaries
        f.write(dict_data)
        pad = z04_off - f.tell()
        if pad > 0:
            f.write(b"\x00" * pad)

        # Z04, Z05, Z07 frames
        f.write(z04_data)
        f.write(z05_data)
        f.write(z07_data)

    total_size = os.path.getsize(output_path)
    print(f"\nTotal: {total_size / 1024 / 1024:.1f} MB -> {output_path}", flush=True)
    print(
        f"  Z04: {z04_off} offset, {len(z04_data)} compressed, {z04_decomp} decompressed",
        flush=True,
    )
    print(
        f"  Z05: {z05_off} offset, {len(z05_data)} compressed, {z05_decomp} decompressed",
        flush=True,
    )
    print(
        f"  Z07: {z07_off} offset, {len(z07_data)} compressed, {z07_decomp} decompressed",
        flush=True,
    )


# ===========================================================================
# Main
# ===========================================================================


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.osm.pbf> <output.ptiles>")
        sys.exit(1)

    pbf_path = sys.argv[1]
    output_path = sys.argv[2]
    t0 = time.time()

    roads = extract_roads(pbf_path)

    # Build each zoom band
    print("Building Z04 (highways, simplified 500m)...", flush=True)
    z04_data, z04_decomp, z04_count, d04 = build_zoom_band(roads, 500, 2, b"", "Z04")

    print("Building Z05 (all roads, simplified 200m)...", flush=True)
    z05_data, z05_decomp, z05_count, d05 = build_zoom_band(roads, 200, 15, b"", "Z05")

    print("Building Z07 (all roads, full precision)...", flush=True)
    z07_data, z07_decomp, z07_count, d07 = build_zoom_band(roads, 0, 15, b"", "Z07")

    write_ptiles(
        output_path,
        z04_data,
        z04_decomp,
        z04_count,
        z05_data,
        z05_decomp,
        z05_count,
        z07_data,
        z07_decomp,
        z07_count,
        [d04, d05, d07],
    )

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
