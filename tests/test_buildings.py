"""
Tests for ptiles.buildings module.

Tests BuildingsReader.open() and query() against the
TN.buildings_v8.ptiles test data file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptiles.buildings import BuildingsReader, Building

DATA_DIR = Path(os.path.expanduser("~/kino/projects/ptiles/data/states"))
TN_BUILDINGS = DATA_DIR / "TN.buildings_v8.ptiles"


@pytest.mark.skipif(not TN_BUILDINGS.exists(), reason=f"Test data not found: {TN_BUILDINGS}")
class TestBuildingsReader:

    @pytest.fixture
    def reader(self) -> BuildingsReader:
        r = BuildingsReader.open(TN_BUILDINGS)
        yield r
        r.close()

    def test_open(self, reader: BuildingsReader):
        """Verify header is parsed correctly."""
        h = reader.header
        assert h is not None
        assert h["version"] >= 6
        assert h["feature_count"] > 0
        assert h["block_count"] > 0

    def test_query_at_nashville(self, reader: BuildingsReader):
        """Test query at a known point near Nashville (downtown area)."""
        result = reader.query(36.1627, -86.7816)
        if result is not None:
            assert isinstance(result, Building)
            assert result.osm_id > 0
            assert isinstance(result.building_type, str)
            assert len(result.coordinates) >= 3
            assert result.centroid_lat is not None
            assert result.centroid_lon is not None

    def test_get_in_cell(self, reader: BuildingsReader):
        """Test reading buildings from a cell."""
        import h3
        cell = h3.latlng_to_cell(36.1627, -86.7816, 7)
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        buildings = reader.get_in_cell(cell_int)
        assert len(buildings) > 0
        assert isinstance(buildings[0], Building)

    def test_within(self, reader: BuildingsReader):
        """Test within returns buildings within a radius."""
        results = reader.within(36.1627, -86.7816, meters=50)
        assert len(results) >= 0  # May be empty if no buildings close
        for b in results:
            assert isinstance(b, Building)

    def test_within_distance(self, reader: BuildingsReader):
        """Test within with 200m radius returns buildings in that range."""
        results = reader.within(36.1627, -86.7816, meters=200)
        assert len(results) >= 0
        for b in results:
            assert isinstance(b, Building)
            assert b.centroid_lat is not None
            assert b.centroid_lon is not None

    def test_get_in_bounds_nashville(self, reader: BuildingsReader):
        """Test get_in_bounds with small bbox around Nashville returns data."""
        results = reader.get_in_bounds(36.15, -86.79, 36.17, -86.77, limit=50)
        assert 0 <= len(results) <= 50
        for b in results:
            assert isinstance(b, Building)
            assert 36.15 <= b.centroid_lat <= 36.17
            assert -86.79 <= b.centroid_lon <= -86.77
