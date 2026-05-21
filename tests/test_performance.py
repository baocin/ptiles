"""
Performance benchmarks for ptiles package using pytest-benchmark.

Benchmarks against TN state data files at
~/kino/projects/ptiles/data/states/.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import h3
import pytest

from ptiles.buildings import BuildingsReader
from ptiles.business import BusinessReader
from ptiles.codec import HEADER_STRUCT, binary_search_index
from ptiles.composite import PtilesClient
from ptiles.roads import RoadsReader
from ptiles.water import WaterReader

DATA_DIR = Path(os.path.expanduser("~/kino/projects/ptiles/data/states"))
TN_BUSINESS = DATA_DIR / "TN.business.ptiles"
TN_ROADS = DATA_DIR / "TN.roads.ptiles"
TN_BUILDINGS = DATA_DIR / "TN.buildings_v8.ptiles"
TN_WATER = DATA_DIR / "TN.water.ptiles"


def parse_header(raw: bytes) -> dict:
    """Parse a 256-byte PTiles header from raw bytes (for benchmarking)."""
    vals = HEADER_STRUCT.unpack(raw[:256])
    return {
        "magic": vals[0],
        "version": vals[2],
        "min_lat": vals[3],
        "min_lon": vals[4],
        "max_lat": vals[5],
        "max_lon": vals[6],
        "feature_count": vals[7],
        "block_count": vals[8],
        "dict_offset": vals[9],
        "dict_length": vals[10],
        "index_offset": vals[11],
        "index_length": vals[12],
        "blocks_offset": vals[13],
        "aux_offset": vals[14],
        "aux_length": vals[15],
    }


def _cold_open_and_query_all() -> None:
    """Cold-open all 4 layers and query each at a Nashville point."""
    lat, lon = 36.1627, -86.7816
    client = PtilesClient.open_state("TN", str(DATA_DIR))
    try:
        if client.buildings:
            client.buildings.query(lat, lon)
        if client.roads:
            client.roads.nearest(lat, lon, radius_meters=100)
            client.roads.nearest_n(lat, lon, n=5, radius_meters=100)
        if client.water:
            cell = int(h3.latlng_to_cell(lat, lon, 7), 16) if isinstance(
                h3.latlng_to_cell(lat, lon, 7), str
            ) else h3.latlng_to_cell(lat, lon, 7)
            client.water.get_in_cell(cell)
        if client.business:
            client.business.nearby(
                lat, lon,
                radius_meters=500, limit=5, category_prefix=None, exclude_closed=False,
            )
    finally:
        client.close()


# ---- Skip if any required data file is missing ----

_required = [TN_BUSINESS, TN_ROADS, TN_BUILDINGS, TN_WATER]
_missing = [str(p) for p in _required if not p.exists()]

skip_if_missing = pytest.mark.skipif(
    bool(_missing),
    reason=f"Required data files not found: {_missing}",
)


# =============================================
# Benchmark 1: Cold open business file
# =============================================

@skip_if_missing
def test_bench_cold_open_business(benchmark):
    """Measure just opening a .business.ptiles file."""
    benchmark(lambda: BusinessReader.open(TN_BUSINESS))


# =============================================
# Benchmark 2: Business nearby query
# =============================================

@skip_if_missing
def test_bench_business_nearby(benchmark):
    """Benchmark BusinessReader.nearby() near Nashville."""
    reader = BusinessReader.open(TN_BUSINESS)
    try:
        benchmark(
            reader.nearby, 36.1627, -86.7816,
            radius_meters=1000.0, limit=10, category_prefix=None, exclude_closed=False,
        )
    finally:
        reader.close()


# =============================================
# Benchmark 3: Road nearest (single)
# =============================================

@skip_if_missing
def test_bench_road_nearest(benchmark):
    """Benchmark RoadsReader.nearest() near Nashville."""
    reader = RoadsReader.open(TN_ROADS)
    try:
        benchmark(
            reader.nearest, 36.1627, -86.7816,
            radius_meters=100.0, profile=None, rings=1,
        )
    finally:
        reader.close()


# =============================================
# Benchmark 4: Road nearest_n (5 results)
# =============================================

@skip_if_missing
def test_bench_road_nearest_5(benchmark):
    """Benchmark RoadsReader.nearest_n() for 5 nearest roads."""
    reader = RoadsReader.open(TN_ROADS)
    try:
        benchmark(
            reader.nearest_n, 36.1627, -86.7816, 5,
            radius_meters=100.0, profile=None,
        )
    finally:
        reader.close()


# =============================================
# Benchmark 5: Building query (point-in-polygon)
# =============================================

@skip_if_missing
def test_bench_building_query(benchmark):
    """Benchmark BuildingsReader.query() near Nashville."""
    reader = BuildingsReader.open(TN_BUILDINGS)
    try:
        benchmark(reader.query, 36.1627, -86.7816)
    finally:
        reader.close()


# =============================================
# Benchmark 6: Building within (radius search)
# =============================================

@skip_if_missing
def test_bench_building_within(benchmark):
    """Benchmark BuildingsReader.within() 200m radius."""
    reader = BuildingsReader.open(TN_BUILDINGS)
    try:
        benchmark(reader.within, 36.1627, -86.7816, 200.0)
    finally:
        reader.close()


# =============================================
# Benchmark 7: Water get_in_cell
# =============================================

@skip_if_missing
def test_bench_water_get_in_cell(benchmark):
    """Benchmark WaterReader.get_in_cell() for a Nashville cell."""
    reader = WaterReader.open(TN_WATER)
    try:
        cell = h3.latlng_to_cell(36.1627, -86.7816, 7)
        cell = int(cell, 16) if isinstance(cell, str) else cell
        benchmark(reader.get_in_cell, cell)
    finally:
        reader.close()


# =============================================
# Benchmark 8: Composite query_point (warm)
# =============================================

@skip_if_missing
def test_bench_composite_query_point(benchmark):
    """Benchmark PtilesClient.query_point() near Nashville (warm client)."""
    client = PtilesClient.open_state("TN", str(DATA_DIR))
    try:
        benchmark(
            client.query_point, 36.1627, -86.7816,
        )
    finally:
        client.close()


# =============================================
# Benchmark 9: Composite cold open
# =============================================

@skip_if_missing
def test_bench_composite_cold_open(benchmark):
    """Benchmark cold opening PtilesClient for TN."""
    benchmark(lambda: PtilesClient.open_state("TN", str(DATA_DIR)))


# =============================================
# Benchmark 10: Index lookup
# =============================================

@skip_if_missing
def test_bench_index_lookup(benchmark):
    """Benchmark binary search in the spatial index."""
    reader = RoadsReader.open(TN_ROADS)
    try:
        # Get the first cell from the reader's index
        first_cell = reader._index[0]["h3_cell"]  # type: ignore[attr-defined]
        benchmark(binary_search_index, reader._index, first_cell)  # type: ignore[attr-defined]
    finally:
        reader.close()


# =============================================
# Benchmark 11: Header parse from raw bytes
# =============================================

@skip_if_missing
def test_bench_header_parse(benchmark):
    """Benchmark parsing 256-byte PTiles header from raw bytes."""
    with open(TN_ROADS, "rb") as f:
        raw = f.read(256)
    benchmark(lambda: parse_header(raw))


# =============================================
# Benchmark 12: Cross-file full context lookup
# =============================================

@skip_if_missing
def test_bench_cross_file_all_layers(benchmark):
    """Cold-open all 4 layers and query each in sequence (no warmup).

    Simulates a full context lookup at a GPS point.
    """
    benchmark(_cold_open_and_query_all)
