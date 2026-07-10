#!/usr/bin/env python3
"""
Build a single national US.parquet from OSM PBFs + Overture Places.

Reads all 51 state PBFs (downloading if needed) + Overture Places Parquet files,
extracts roads/water/buildings/parks/rail/places/address/business,
and writes a single Parquet file with one row group per layer.

Usage:
    # Build full national parquet (downloads missing PBFs)
    uv run --with osmium --with pyarrow --with pandas --with shapely --with h3 --with zstandard \
        python build_national_parquet.py

    # Just TN test
    uv run --with osmium --with pyarrow --with pandas --with shapely --with h3 --with zstandard \
        python build_national_parquet.py --test

    # Resume from checkpoint
    uv run --with osmium --with pyarrow --with pandas --with shapely --with h3 --with zstandard \
        python build_national_parquet.py --resume

Output:
    /home/aoi/kino/projects/ptiles/data/parquet/US.parquet
"""

import sys
import os
import struct
import time
import json
import gc
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

# Add scripts dir for imports
sys.path.insert(0, os.path.dirname(__file__))

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd
import h3
import osmium

from states import STATES, get_state

# ===========================================================================
# Configuration
# ===========================================================================

PBF_DIR = Path("/home/aoi/kino/projects/ptiles/data/pbfs")
OVERTURE_DIR = Path(os.path.expanduser("~/overture-2026-04-15.0/places"))
OUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/parquet")
CHECKPOINT_DIR = Path("/home/aoi/kino/projects/ptiles/data/parquet/checkpoints")
ADMIN_DATA_DIR = Path("/mnt/core/timeline-ptiles-cache/admin_data")

H3_RES = 7

# Road class mapping (same as build_roads.py)
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

# Water type enum
WATER_TYPES = {
    "lake": "lake",
    "reservoir": "reservoir",
    "pond": "pond",
    "river": "river",
    "stream": "stream",
    "creek": "creek",
    "canal": "canal",
    "drain": "drain",
    "bay": "bay",
    "ocean": "ocean",
    "wetland": "wetland",
    "marsh": "marsh",
    "swamp": "swamp",
    "estuary": "estuary",
}

# Rail types
TRACK_TYPES = {
    "rail",
    "tram",
    "light_rail",
    "subway",
    "monorail",
    "narrow_gauge",
    "funicular",
    "preserved",
}
STATION_TYPES = {"station", "halt", "tram_stop", "subway_entrance"}

# Place types
PLACE_TYPES = {
    "city",
    "town",
    "village",
    "hamlet",
    "neighborhood",
    "suburb",
    "borough",
    "quarter",
    "isolated_dwelling",
}

# Park tags
PARK_TAGS_LEISURE = {
    "park",
    "golf_course",
    "nature_reserve",
    "recreation_ground",
    "playground",
}
PARK_TAGS_BOUNDARY = {"national_park", "protected_area"}

# ===========================================================================
# Schema definitions
# ===========================================================================

SCHEMA_ROADS = pa.schema(
    [
        ("osm_id", pa.int64()),
        ("state_abbr", pa.string()),
        ("highway", pa.string()),
        ("name", pa.string()),
        ("ref", pa.string()),
        ("oneway", pa.bool_()),
        ("maxspeed", pa.int32()),
        ("lanes", pa.int32()),
        ("surface", pa.string()),
        ("bridge", pa.bool_()),
        ("tunnel", pa.bool_()),
        ("lon_min", pa.float64()),
        ("lat_min", pa.float64()),
        ("lon_max", pa.float64()),
        ("lat_max", pa.float64()),
        ("geometry", pa.binary()),  # WKB
    ]
)

SCHEMA_WATER = pa.schema(
    [
        ("osm_id", pa.int64()),
        ("state_abbr", pa.string()),
        ("water_type", pa.string()),
        ("name", pa.string()),
        ("width", pa.int32()),
        ("lon_min", pa.float64()),
        ("lat_min", pa.float64()),
        ("lon_max", pa.float64()),
        ("lat_max", pa.float64()),
        ("geometry", pa.binary()),  # WKB
    ]
)

SCHEMA_BUILDINGS = pa.schema(
    [
        ("osm_id", pa.int64()),
        ("state_abbr", pa.string()),
        ("btype", pa.string()),
        ("name", pa.string()),
        ("height_m", pa.float32()),
        ("lon_min", pa.float64()),
        ("lat_min", pa.float64()),
        ("lon_max", pa.float64()),
        ("lat_max", pa.float64()),
        ("geometry", pa.binary()),  # WKB
    ]
)

SCHEMA_BUSINESS = pa.schema(
    [
        ("unified_id", pa.int64()),
        ("state_abbr", pa.string()),
        ("name", pa.string()),
        ("brand", pa.string()),
        ("category", pa.string()),
        ("phone", pa.string()),
        ("website", pa.string()),
        ("address", pa.string()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("source", pa.string()),
        ("source_id", pa.string()),
        ("confidence", pa.int16()),
    ]
)

SCHEMA_PARKS = pa.schema(
    [
        ("osm_id", pa.int64()),
        ("state_abbr", pa.string()),
        ("park_type", pa.string()),
        ("name", pa.string()),
        ("lon_min", pa.float64()),
        ("lat_min", pa.float64()),
        ("lon_max", pa.float64()),
        ("lat_max", pa.float64()),
        ("geometry", pa.binary()),  # WKB
    ]
)

SCHEMA_RAIL = pa.schema(
    [
        ("osm_id", pa.int64()),
        ("state_abbr", pa.string()),
        ("rail_type", pa.string()),
        ("geom_type", pa.utf8()),
        ("name", pa.string()),
        ("lon_min", pa.float64()),
        ("lat_min", pa.float64()),
        ("lon_max", pa.float64()),
        ("lat_max", pa.float64()),
        ("geometry", pa.binary()),  # WKB
    ]
)

SCHEMA_PLACES = pa.schema(
    [
        ("osm_id", pa.int64()),
        ("state_abbr", pa.string()),
        ("place_type", pa.string()),
        ("name", pa.string()),
        ("alt_name", pa.string()),
        ("population", pa.int32()),
        ("admin_level", pa.int16()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
    ]
)

SCHEMA_ADDRESS = pa.schema(
    [
        ("osm_id", pa.int64()),
        ("state_abbr", pa.string()),
        ("street", pa.string()),
        ("housenumber", pa.string()),
        ("city", pa.string()),
        ("postcode", pa.string()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
    ]
)

SCHEMA_ADMIN = pa.schema(
    [
        ("h3_cell", pa.int64()),
        ("state_fips", pa.string()),
        ("state_name", pa.string()),
        ("county_fips", pa.string()),
        ("county_name", pa.string()),
        ("zcta", pa.string()),
        ("timezone", pa.string()),
    ]
)

# ===========================================================================
# WKB helpers
# ===========================================================================

WKB_POINT = 1
WKB_LINESTRING = 2
WKB_POLYGON = 3
WKB_MULTIPOLYGON = 6
WKB_MULTILINESTRING = 5


def wkb_bounds(wkb):
    """Extract (lon_min, lat_min, lon_max, lat_max) from WKB geometry.
    Handles POINT, LINESTRING, POLYGON, MULTIPOLYGON, MULTILINESTRING.
    Returns None on invalid/corrupt WKB."""
    if not wkb or len(wkb) < 9:
        return None
    try:
        byte_order = wkb[0]
        little = byte_order == 1
        geom_type = struct.unpack_from("<I" if little else ">I", wkb, 1)[0]

        off = 5
        lon_min = lat_min = float("inf")
        lon_max = lat_max = float("-inf")

        def read_point(data, offset, le):
            lo = struct.unpack_from("<d" if le else ">d", data, offset)[0]
            la = struct.unpack_from("<d" if le else ">d", data, offset + 8)[0]
            return lo, la, offset + 16

        def scan_ring(data, offset, le):
            nonlocal lon_min, lat_min, lon_max, lat_max
            npts = struct.unpack_from("<I" if le else ">I", data, offset)[0]
            offset += 4
            for _ in range(npts):
                lo, la, offset = read_point(data, offset, le)
                if lo < lon_min:
                    lon_min = lo
                if lo > lon_max:
                    lon_max = lo
                if la < lat_min:
                    lat_min = la
                if la > lat_max:
                    lat_max = la
            return offset

        if geom_type == WKB_POINT:
            lo, la, _ = read_point(wkb, off, little)
            return (lo, la, lo, la)

        elif geom_type == WKB_LINESTRING:
            npts = struct.unpack_from("<I" if little else ">I", wkb, off)[0]
            off += 4
            for _ in range(npts):
                lo, la, off = read_point(wkb, off, little)
                if lo < lon_min:
                    lon_min = lo
                if lo > lon_max:
                    lon_max = lo
                if la < lat_min:
                    lat_min = la
                if la > lat_max:
                    lat_max = la
            return (lon_min, lat_min, lon_max, lat_max)

        elif geom_type == WKB_POLYGON:
            nrings = struct.unpack_from("<I" if little else ">I", wkb, off)[0]
            off += 4
            for _ in range(nrings):
                off = scan_ring(wkb, off, little)
            return (lon_min, lat_min, lon_max, lat_max)

        elif geom_type == WKB_MULTIPOLYGON:
            nparts = struct.unpack_from("<I" if little else ">I", wkb, off)[0]
            off += 4
            for _ in range(nparts):
                # Skip sub-geometry header (byteOrder + type)
                sub_order = wkb[off]
                sub_le = sub_order == 1
                off += 5
                nrings = struct.unpack_from("<I" if sub_le else ">I", wkb, off)[0]
                off += 4
                for _ in range(nrings):
                    off = scan_ring(wkb, off, sub_le)
            return (lon_min, lat_min, lon_max, lat_max)

        elif geom_type == WKB_MULTILINESTRING:
            nparts = struct.unpack_from("<I" if little else ">I", wkb, off)[0]
            off += 5  # skip byteOrder + type of first sub-geometry
            for _ in range(nparts):
                npts = struct.unpack_from("<I" if little else ">I", wkb, off)[0]
                off += 4
                for _ in range(npts):
                    lo, la, off = read_point(wkb, off, little)
                    if lo < lon_min:
                        lon_min = lo
                    if lo > lon_max:
                        lon_max = lo
                    if la < lat_min:
                        lat_min = la
                    if la > lat_max:
                        lat_max = la
            return (lon_min, lat_min, lon_max, lat_max)

        return None
    except Exception:
        return None


def wkb_point(lon, lat):
    """Build WKB POINT."""
    buf = bytearray(21)
    # WKB: byteOrder(1) + type(u32) + point(2×f64)
    struct.pack_into("<B", buf, 0, 1)  # little endian
    struct.pack_into("<I", buf, 1, WKB_POINT)
    struct.pack_into("<dd", buf, 5, lon, lat)
    return bytes(buf)


def wkb_linestring(coords):
    """Build WKB LINESTRING from [(lon,lat),...]."""
    n = len(coords)
    if n == 0:
        return b""
    buf = bytearray(9 + 16 * n)
    struct.pack_into("<B", buf, 0, 1)  # little endian
    struct.pack_into("<I", buf, 1, WKB_LINESTRING)
    struct.pack_into("<I", buf, 5, n)
    off = 9
    for lon, lat in coords:
        struct.pack_into("<dd", buf, off, float(lon), float(lat))
        off += 16
    return bytes(buf[:off])


def wkb_polygon(ring):
    """Build WKB POLYGON from a closed ring [(lon,lat),...] (first == last)."""
    n = len(ring)
    if n < 3:
        return b""
    buf = bytearray(9 + 4 + 16 * n)
    struct.pack_into("<B", buf, 0, 1)  # little endian
    struct.pack_into("<I", buf, 1, WKB_POLYGON)
    struct.pack_into("<I", buf, 5, 1)  # 1 ring
    struct.pack_into("<I", buf, 9, n)
    off = 13
    for lon, lat in ring:
        struct.pack_into("<dd", buf, off, float(lon), float(lat))
        off += 16
    return bytes(buf[:off])


# ===========================================================================
# OSM Extraction — unified osmium handler
# ===========================================================================


class UnifiedOSMExtractor(osmium.SimpleHandler):
    """Single pass through a PBF file extracting all OSM layers."""

    def __init__(self, state_abbr, bbox=None):
        super().__init__()
        self.state_abbr = state_abbr
        self.bbox = bbox  # (min_lon, min_lat, max_lon, max_lat) or None
        self.roads = []
        self.buildings = []
        self.water_areas = []
        self.water_ways = []
        self.parks = []
        self.rail = []
        self.places = []
        self.addresses = []
        self._count = 0

    def _in_bbox(self, lon, lat):
        if not self.bbox:
            return True
        min_lon, min_lat, max_lon, max_lat = self.bbox
        return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat

    def way(self, w):
        tags = dict((t.k, t.v) for t in w.tags if t.v)

        # Roads (highway tag)
        highway = tags.get("highway")
        if highway and highway not in ("proposed", "construction", "raceway", "escape"):
            try:
                coords = [(n.lon, n.lat) for n in w.nodes]
            except osmium.InvalidLocationError:
                coords = []
            if len(coords) >= 2 and self._in_bbox(coords[0][0], coords[0][1]):
                self.roads.append(
                    {
                        "osm_id": w.id,
                        "state_abbr": self.state_abbr,
                        "highway": highway,
                        "name": tags.get("name"),
                        "ref": tags.get("ref"),
                        "oneway": tags.get("oneway") == "yes",
                        "maxspeed": _parse_speed(tags.get("maxspeed")),
                        "lanes": _parse_int(tags.get("lanes")),
                        "surface": tags.get("surface", ""),
                        "bridge": tags.get("bridge") == "yes",
                        "tunnel": tags.get("tunnel") == "yes",
                    }
                )
                row = self.roads[-1]
                geom = wkb_linestring(coords)
                row["geometry"] = geom
                bbox = wkb_bounds(geom)
                if bbox:
                    row["lon_min"], row["lat_min"], row["lon_max"], row["lat_max"] = (
                        bbox
                    )
                else:
                    row["lon_min"] = row["lat_min"] = row["lon_max"] = row[
                        "lat_max"
                    ] = 0.0

        # Waterway (linestring water features)
        waterway = tags.get("waterway")
        if waterway and tags.get("waterway") not in (
            "dock",
            "boatyard",
            "dam",
            "weir",
            "lock_gate",
        ):
            try:
                coords = [(n.lon, n.lat) for n in w.nodes]
            except osmium.InvalidLocationError:
                coords = []
            if len(coords) >= 2 and self._in_bbox(coords[0][0], coords[0][1]):
                self.water_ways.append(
                    {
                        "osm_id": w.id,
                        "state_abbr": self.state_abbr,
                        "water_type": waterway,
                        "name": tags.get("name", ""),
                        "width": _parse_int(tags.get("width")),
                    }
                )
                row = self.water_ways[-1]
                geom = wkb_linestring(coords)
                row["geometry"] = geom
                bbox = wkb_bounds(geom)
                if bbox:
                    row["lon_min"], row["lat_min"], row["lon_max"], row["lat_max"] = (
                        bbox
                    )
                else:
                    row["lon_min"] = row["lat_min"] = row["lon_max"] = row[
                        "lat_max"
                    ] = 0.0

        # Addresses on ways (buildings with addr info)
        hn = tags.get("addr:housenumber")
        if hn and tags.get("building"):
            try:
                loc = w.nodes[0]
                lon, lat = loc.lon, loc.lat
            except (IndexError, osmium.InvalidLocationError):
                lon = lat = None
            if lon is not None and self._in_bbox(lon, lat):
                self.addresses.append(
                    {
                        "osm_id": w.id,
                        "state_abbr": self.state_abbr,
                        "street": tags.get("addr:street", ""),
                        "housenumber": hn,
                        "city": tags.get("addr:city", ""),
                        "postcode": tags.get("addr:postcode", ""),
                        "lat": lat,
                        "lon": lon,
                    }
                )

        # Parks (polygon from closed ways)
        if self._is_park(tags):
            coords = []
            try:
                coords = [(n.lon, n.lat) for n in w.nodes]
            except osmium.InvalidLocationError:
                pass
            if len(coords) >= 3 and self._in_bbox(coords[0][0], coords[0][1]):
                # Close ring if open
                if coords and coords[0] != coords[-1]:
                    coords.append(coords[0])
                park_type = self._park_type(tags)
                self.parks.append(
                    {
                        "osm_id": w.id,
                        "state_abbr": self.state_abbr,
                        "park_type": park_type,
                        "name": tags.get("name", ""),
                    }
                )
                row = self.parks[-1]
                geom = wkb_polygon(coords)
                row["geometry"] = geom
                bbox = wkb_bounds(geom)
                if bbox:
                    row["lon_min"], row["lat_min"], row["lon_max"], row["lat_max"] = (
                        bbox
                    )
                else:
                    row["lon_min"] = row["lat_min"] = row["lon_max"] = row[
                        "lat_max"
                    ] = 0.0

        # Rail
        railway = tags.get("railway")
        if railway in TRACK_TYPES:
            try:
                coords = [(n.lon, n.lat) for n in w.nodes]
            except osmium.InvalidLocationError:
                coords = []
            if len(coords) >= 2 and self._in_bbox(coords[0][0], coords[0][1]):
                name = tags.get("name", "")
                self.rail.append(
                    {
                        "osm_id": w.id,
                        "state_abbr": self.state_abbr,
                        "rail_type": railway,
                        "geom_type": "line",
                        "name": name,
                    }
                )
                row = self.rail[-1]
                geom = wkb_linestring(coords)
                row["geometry"] = geom
                bbox = wkb_bounds(geom)
                if bbox:
                    row["lon_min"], row["lat_min"], row["lon_max"], row["lat_max"] = (
                        bbox
                    )
                else:
                    row["lon_min"] = row["lat_min"] = row["lon_max"] = row[
                        "lat_max"
                    ] = 0.0

    def area(self, a):
        """Extract water bodies and building footprints from OSM areas."""
        tags = dict((t.k, t.v) for t in a.tags if t.v)

        # Water bodies (natural=water, water=*, natural=coastline)
        if tags.get("natural") in ("water", "coastline") or tags.get("water"):
            for ring in a.outer_rings():
                coords = [(n.lon, n.lat) for n in ring]
                if len(coords) >= 3 and self._in_bbox(coords[0][0], coords[0][1]):
                    if coords[0] != coords[-1]:
                        coords.append(coords[0])
                    water_type = tags.get("water") or tags.get("natural", "unknown")
                    self.water_areas.append(
                        {
                            "osm_id": a.orig_id(),
                            "state_abbr": self.state_abbr,
                            "water_type": water_type,
                            "name": tags.get("name", ""),
                            "width": None,
                        }
                    )
                    row = self.water_areas[-1]
                    geom = wkb_polygon(coords)
                    row["geometry"] = geom
                    bbox = wkb_bounds(geom)
                    if bbox:
                        (
                            row["lon_min"],
                            row["lat_min"],
                            row["lon_max"],
                            row["lat_max"],
                        ) = bbox
                    else:
                        row["lon_min"] = row["lat_min"] = row["lon_max"] = row[
                            "lat_max"
                        ] = 0.0
                    break  # One ring is enough for most cases

        # Buildings
        if tags.get("building"):
            for ring in a.outer_rings():
                coords = [(n.lon, n.lat) for n in ring]
                if len(coords) >= 3 and self._in_bbox(coords[0][0], coords[0][1]):
                    if coords[0] != coords[-1]:
                        coords.append(coords[0])
                    height = None
                    try:
                        hv = tags.get("height")
                        if hv:
                            height = float(hv.rstrip("m "))
                    except (ValueError, AttributeError):
                        pass
                    self.buildings.append(
                        {
                            "osm_id": a.orig_id(),
                            "state_abbr": self.state_abbr,
                            "btype": tags.get("building", "yes"),
                            "name": tags.get("name", ""),
                            "height_m": height,
                        }
                    )
                    row = self.buildings[-1]
                    geom = wkb_polygon(coords)
                    row["geometry"] = geom
                    bbox = wkb_bounds(geom)
                    if bbox:
                        (
                            row["lon_min"],
                            row["lat_min"],
                            row["lon_max"],
                            row["lat_max"],
                        ) = bbox
                    else:
                        row["lon_min"] = row["lat_min"] = row["lon_max"] = row[
                            "lat_max"
                        ] = 0.0
                    break

    def node(self, n):
        tags = dict((t.k, t.v) for t in n.tags if t.v)

        if not self._in_bbox(n.lon, n.lat):
            return

        # Places
        place = tags.get("place")
        if place and place in PLACE_TYPES and tags.get("name"):
            pop = _parse_int(tags.get("population"))
            al = _parse_int(tags.get("admin_level"))
            self.places.append(
                {
                    "osm_id": n.id,
                    "state_abbr": self.state_abbr,
                    "place_type": place,
                    "name": tags.get("name", ""),
                    "alt_name": tags.get("alt_name", ""),
                    "population": pop if pop else 0,
                    "admin_level": al if al else 0,
                    "lat": n.lat,
                    "lon": n.lon,
                }
            )

        # Addresses on nodes
        hn = tags.get("addr:housenumber")
        if hn:
            self.addresses.append(
                {
                    "osm_id": n.id,
                    "state_abbr": self.state_abbr,
                    "street": tags.get("addr:street", ""),
                    "housenumber": hn,
                    "city": tags.get("addr:city", ""),
                    "postcode": tags.get("addr:postcode", ""),
                    "lat": n.lat,
                    "lon": n.lon,
                }
            )

        # Rail stations
        railway = tags.get("railway")
        if railway in STATION_TYPES:
            self.rail.append(
                {
                    "osm_id": n.id,
                    "state_abbr": self.state_abbr,
                    "rail_type": railway,
                    "geom_type": "station",
                    "name": tags.get("name", ""),
                }
            )
            row = self.rail[-1]
            geom = wkb_point(n.lon, n.lat)
            row["geometry"] = geom
            row["lon_min"] = row["lon_max"] = n.lon
            row["lat_min"] = row["lat_max"] = n.lat

    @staticmethod
    def _is_park(tags):
        if tags.get("leisure") in PARK_TAGS_LEISURE:
            return True
        if tags.get("boundary") in PARK_TAGS_BOUNDARY:
            return True
        return False

    @staticmethod
    def _park_type(tags):
        l = tags.get("leisure")
        if l in PARK_TAGS_LEISURE:
            return l
        b = tags.get("boundary")
        if b in PARK_TAGS_BOUNDARY:
            return b
        return "park"


def _parse_speed(v):
    if not v:
        return 0
    v = v.strip()
    try:
        return int(v.rstrip(" mph"))
    except ValueError:
        # "55 mph" -> int("55 ") -> strips space
        try:
            return int(v.split()[0])
        except (ValueError, IndexError):
            return 0


def _parse_int(v):
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


# ===========================================================================
# Overture Places extraction
# ===========================================================================

US_BBOX = (-125.0, 24.0, -66.0, 50.0)


def extract_business(progress_callback=None):
    """Read Overture Places Parquet files, filter to CONUS, return DataFrame."""
    print("\nReading Overture Places...", flush=True)
    t0 = time.time()

    all_rows = []
    files = sorted(OVERTURE_DIR.glob("*.zstd.parquet"))

    for fname in files:
        t1 = time.time()
        table = pq.read_table(str(fname))
        n = len(table)

        # Filter to US addresses
        addr_col = table.column("addresses").to_pylist()
        geom_col = table.column("geometry").to_pylist()

        mask = []
        for i in range(n):
            addrs = addr_col[i]
            if not addrs:
                continue
            if addrs[0].get("country", "") != "US":
                continue
            geom = geom_col[i]
            if len(geom) < 21:
                continue
            lon = struct.unpack_from("<d", geom, 5)[0]
            lat = struct.unpack_from("<d", geom, 13)[0]
            if US_BBOX[1] <= lat <= US_BBOX[3] and US_BBOX[0] <= lon <= US_BBOX[2]:
                mask.append((i, lon, lat, addrs[0].get("freeform", "") or ""))

        if not mask:
            continue

        # Read full structured columns for matching rows
        id_col = table.column("id").to_pylist()
        names_col = table.column("names").to_pylist()
        cats_col = table.column("categories").to_pylist()
        phones_col = table.column("phones").to_pylist()
        websites_col = table.column("websites").to_pylist()
        brand_col = table.column("brand").to_pylist()
        op_col = table.column("operating_status").to_pylist()

        for idx, lon, lat, addr in mask:
            names = names_col[idx]
            cats = cats_col[idx]
            brand = brand_col[idx]

            brand_name = ""
            if brand:
                bn = brand.get("names", {})
                if bn:
                    brand_name = bn.get("primary", "")

            cat = cats.get("primary", "") if cats else ""

            all_rows.append(
                {
                    "unified_id": hash(id_col[idx]) & ((1 << 63) - 1),
                    "state_abbr": "",
                    "name": names.get("primary", "") if names else "",
                    "brand": brand_name,
                    "category": cat,
                    "phone": phones_col[idx][0] if phones_col[idx] else "",
                    "website": websites_col[idx][0] if websites_col[idx] else "",
                    "address": addr,
                    "lat": lat,
                    "lon": lon,
                    "source": "Overture",
                    "source_id": str(id_col[idx]),
                    "confidence": 80,
                }
            )

        basename = fname.name
        print(
            f"  {basename}: {len(mask)} US places in {time.time() - t1:.1f}s",
            flush=True,
        )

        # Free memory
        del table, addr_col, geom_col, id_col, names_col, cats_col
        del phones_col, websites_col, brand_col, op_col

    # Assign state abbreviations from lat/lon
    for row in all_rows:
        row["state_abbr"] = latlon_to_state(row["lat"], row["lon"])

    df = pd.DataFrame(all_rows)
    print(f"\n  Total US business: {len(df)} in {time.time() - t0:.1f}s", flush=True)
    return df


# ===========================================================================
# State abbreviation from lat/lon (simple point-in-polygon via H3)
# ===========================================================================

# Approximate mapping of H3 resolution 2 cells to states
# Built from census shapefiles; fallback to simple bbox check
STATE_H3_CACHE = {}


def latlon_to_state(lat, lon):
    """Return state abbreviation for a lat/lon point."""
    # Use H3 res 4 for coarse state lookup
    cell = h3.latlng_to_cell(lat, lon, 4)
    # Simple bounding-box based lookup
    for s in STATES:
        if s.min_lon <= lon <= s.max_lon and s.min_lat <= lat <= s.max_lat:
            return s.abbr
    return ""


# ===========================================================================
# PBF downloader
# ===========================================================================

GEOFABRIK_URL = "https://download.geofabrik.de/north-america/us"


def ensure_pbf(state_abbr):
    """Find or download per-state PBF. Checks NFS cache and local dir."""
    state = get_state(state_abbr)
    nfs_name = f"{state.name.lower().replace(' ', '-')}.osm.pbf"
    nfs_path = Path("/mnt/core/timeline-ptiles-cache/raw") / nfs_name
    if nfs_path.exists():
        return str(nfs_path)

    local_name = f"{state.name.lower().replace(' ', '-')}-latest.osm.pbf"
    local_path = PBF_DIR / local_name
    if local_path.exists():
        return str(local_path)

    url = f"{GEOFABRIK_URL}/{local_name}"
    print(f"\n  Downloading {state.name} PBF...", flush=True)
    t0 = time.time()
    import urllib.request

    urllib.request.urlretrieve(url, str(local_path))
    print(f"  Downloaded in {time.time() - t0:.1f}s", flush=True)
    return str(local_path)


# ===========================================================================
# Main build
# ===========================================================================


def build_state_rows(state_abbr, pbf_path):
    """Process one state PBF, write per-state checkpoint parquet, return layer counts."""

    print(f"\n{'=' * 60}", flush=True)
    print(f"Processing {state_abbr} ({get_state(state_abbr).name})", flush=True)
    print(f"{'=' * 60}", flush=True)

    t0 = time.time()

    handler = UnifiedOSMExtractor(state_abbr)
    handler.apply_file(pbf_path, locations=True)

    elapsed = time.time() - t0
    print(f"  Extracted in {elapsed:.1f}s", flush=True)
    print(f"    Roads: {len(handler.roads):,}", flush=True)
    print(f"    Water areas: {len(handler.water_areas):,}", flush=True)
    print(f"    Water ways: {len(handler.water_ways):,}", flush=True)
    print(f"    Buildings: {len(handler.buildings):,}", flush=True)
    print(f"    Parks: {len(handler.parks):,}", flush=True)
    print(f"    Rail: {len(handler.rail):,}", flush=True)
    print(f"    Places: {len(handler.places):,}", flush=True)
    print(f"    Addresses: {len(handler.addresses):,}", flush=True)

    # Write per-state checkpoint Parquet files
    state_dir = CHECKPOINT_DIR / state_abbr
    os.makedirs(state_dir, exist_ok=True)

    layer_schemas = {
        "roads": (handler.roads, SCHEMA_ROADS),
        "water": (handler.water_areas + handler.water_ways, SCHEMA_WATER),
        "buildings": (handler.buildings, SCHEMA_BUILDINGS),
        "parks": (handler.parks, SCHEMA_PARKS),
        "rail": (handler.rail, SCHEMA_RAIL),
        "places": (handler.places, SCHEMA_PLACES),
        "addresses": (handler.addresses, SCHEMA_ADDRESS),
    }

    for layer_name, (rows, schema) in layer_schemas.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
        out = state_dir / f"{layer_name}.parquet"
        all_cols = [f.name for f in schema]
        write_stats = {col: True for col in all_cols}
        pq.write_table(
            table,
            str(out),
            compression="ZSTD",
            compression_level=3,
            row_group_size=524288,
            write_statistics=write_stats,
        )

    # Free handler memory
    del handler

    return elapsed


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build national US.parquet")
    parser.add_argument("--test", action="store_true", help="Process only TN")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument(
        "--states", nargs="+", help="Specific state abbreviations to process"
    )
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    os.makedirs(PBF_DIR, exist_ok=True)

    # Determine which states to process
    if args.states:
        state_list = [s.upper() for s in args.states]
    elif args.test:
        state_list = ["TN"]
    else:
        state_list = [
            s.abbr for s in STATES if s.abbr not in ("AK", "HI")
        ]  # skip AK/HI initially

    # Checkpoint tracking — always load existing state
    checkpoint_file = CHECKPOINT_DIR / "completed_states.json"
    if checkpoint_file.exists():
        with open(checkpoint_file) as f:
            completed = set(json.load(f))
    else:
        completed = set()

    # Also discover states with checkpoint dirs that weren't recorded
    for d in CHECKPOINT_DIR.iterdir():
        if d.is_dir() and d.name != "__pycache__":
            if d.name not in completed:
                # Verify it has all 7 layer files
                layers = [
                    "roads",
                    "water",
                    "buildings",
                    "parks",
                    "rail",
                    "places",
                    "addresses",
                ]
                if all((d / f"{l}.parquet").exists() for l in layers):
                    print(f"  Discovered orphan checkpoint: {d.name}", flush=True)
                    completed.add(d.name)

    state_list = [s for s in state_list if s not in completed]

    if not state_list:
        print(
            "All requested states already processed. Skipping OSM extraction.",
            flush=True,
        )
    else:
        print(
            f"\nProcessing {len(state_list)} states: {', '.join(state_list)}",
            flush=True,
        )

    # Process each state
    for i, abbr in enumerate(state_list):
        pbf_path = ensure_pbf(abbr)
        t1 = time.time()
        elapsed = build_state_rows(abbr, pbf_path)

        # Save checkpoint
        completed.add(abbr)
        with open(checkpoint_file, "w") as f:
            json.dump(list(completed), f)

        print(
            f"  {abbr} done in {elapsed:.0f}s, written to {CHECKPOINT_DIR / abbr}",
            flush=True,
        )

        # Force GC every 3 states
        if (i + 1) % 3 == 0:
            gc.collect()

    # Read business data (once, national)
    business_df = extract_business()

    # Merge all checkpoints into final US.parquet
    print("\nMerging checkpoints into US.parquet...", flush=True)

    all_layers = ["roads", "water", "buildings", "parks", "rail", "places", "addresses"]
    layer_schemas = {
        "roads": SCHEMA_ROADS,
        "water": SCHEMA_WATER,
        "buildings": SCHEMA_BUILDINGS,
        "parks": SCHEMA_PARKS,
        "rail": SCHEMA_RAIL,
        "places": SCHEMA_PLACES,
        "addresses": SCHEMA_ADDRESS,
        "business": SCHEMA_BUSINESS,
    }

    merged = {}
    for layer in all_layers:
        t1 = time.time()
        all_dfs = []
        for abbr in state_list:
            cp = CHECKPOINT_DIR / abbr / f"{layer}.parquet"
            if cp.exists():
                df = pd.read_parquet(str(cp))
                if len(df) > 0:
                    all_dfs.append(df)
        if all_dfs:
            full_df = pd.concat(all_dfs, ignore_index=True)
            table = pa.Table.from_pandas(
                full_df, schema=layer_schemas[layer], preserve_index=False
            )
            merged[layer] = table
            print(
                f"  {layer}: {len(full_df):,} rows in {time.time() - t1:.1f}s",
                flush=True,
            )
            del full_df, all_dfs
        else:
            print(f"  {layer}: no data", flush=True)

    # Business
    if len(business_df) > 0:
        merged["business"] = pa.Table.from_pandas(
            business_df, schema=SCHEMA_BUSINESS, preserve_index=False
        )
        print(f"  business: {len(business_df):,} rows", flush=True)

    # Write each layer as a separate Parquet file
    output_dir = OUT_DIR
    print(f"\nWriting layers to {output_dir}...", flush=True)

    layer_names = list(merged.keys())
    for name in layer_names:
        t1 = time.time()
        out_path = output_dir / f"{name}.parquet"
        all_cols = [f.name for f in merged[name].schema]
        write_stats = {col: True for col in all_cols}
        pq.write_table(
            merged[name],
            str(out_path),
            compression="ZSTD",
            compression_level=3,
            row_group_size=524288,
            write_statistics=write_stats,
        )
        sz = out_path.stat().st_size
        print(
            f"  {name}: {len(merged[name]):,} rows, {sz / 1024 / 1024:.1f} MB in {time.time() - t1:.1f}s",
            flush=True,
        )

    # Also write business if present
    if "business" in merged:
        biz_path = output_dir / "business.parquet"
        if not biz_path.exists():
            all_cols = [f.name for f in merged["business"].schema]
            write_stats = {col: True for col in all_cols}
            pq.write_table(
                merged["business"],
                str(biz_path),
                compression="ZSTD",
                compression_level=3,
                row_group_size=524288,
                write_statistics=write_stats,
            )
            sz = biz_path.stat().st_size
            print(
                f"  business: {len(merged['business']):,} rows, {sz / 1024 / 1024:.1f} MB",
                flush=True,
            )

    print(f"\\nDone! Files in {output_dir}", flush=True)


if __name__ == "__main__":
    main()
