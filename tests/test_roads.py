"""
Tests for ptiles.roads module.

Tests RoadsReader.open(), get_in_cell, and nearest() against the
TN.roads.ptiles test data file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptiles.roads import RoadsReader, RoadSegment, NearestRoad

DATA_DIR = Path(os.path.expanduser("~/kino/projects/ptiles/data/states"))
TN_ROADS = DATA_DIR / "TN.roads.ptiles"


@pytest.mark.skipif(not TN_ROADS.exists(), reason=f"Test data not found: {TN_ROADS}")
class TestRoadsReader:

    @pytest.fixture
    def reader(self) -> RoadsReader:
        r = RoadsReader.open(TN_ROADS)
        yield r
        r.close()

    def test_open(self, reader: RoadsReader):
        """Verify header is parsed correctly."""
        h = reader.header
        assert h is not None
        assert h["version"] >= 1
        assert h["feature_count"] > 0
        assert h["block_count"] > 0
        # TN bounds
        assert -90.0 < h["min_lat"] < 40.0
        assert -100.0 < h["min_lon"] < -80.0
        assert 30.0 < h["max_lat"] < 50.0
        assert -82.0 <= h["max_lon"] < -70.0

    def test_get_in_cell(self, reader: RoadsReader):
        """Test reading a cell near Nashville."""
        import h3
        # Nashville
        cell = h3.latlng_to_cell(36.1627, -86.7816, 7)
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        roads = reader.get_in_cell(cell_int)
        assert len(roads) > 0
        assert isinstance(roads[0], RoadSegment)

    def test_nearest_nashville(self, reader: RoadsReader):
        """Test nearest road near Nashville (I-40 area)."""
        result = reader.nearest(36.1627, -86.7816, radius_meters=100)
        assert result is not None
        assert result.distance_meters >= 0
        assert isinstance(result.road, RoadSegment)
        assert result.road.osm_id > 0
        assert result.road.road_class in (
            "motorway", "motorway_link", "trunk", "trunk_link",
            "primary", "primary_link", "secondary", "tertiary",
            "residential", "service", "track", "footway",
        )

    def test_nearest_n(self, reader: RoadsReader):
        """Test nearest_n returns multiple results."""
        results = reader.nearest_n(36.1627, -86.7816, n=3, radius_meters=200)
        assert len(results) >= 1
        assert len(results) <= 3
        # Results should be sorted by distance
        for i in range(len(results) - 1):
            assert results[i].distance_meters <= results[i + 1].distance_meters

    def test_nearest_with_profile(self, reader: RoadsReader):
        """Test nearest with driving profile filter."""
        result = reader.nearest(36.1627, -86.7816, radius_meters=200, profile="driving")
        if result is not None:
            # Should be a drivable road class
            driving_classes = {
                "motorway", "motorway_link", "trunk", "trunk_link",
                "primary", "primary_link", "secondary", "tertiary", "tertiary_link",
            }
            assert result.road.road_class in driving_classes

    def test_nearest_far_away(self, reader: RoadsReader):
        """Test nearest returns None when no road is within radius."""
        # Middle of nowhere in Nevada — TN file won't have it
        result = reader.nearest(39.0, -119.0, radius_meters=100)
        assert result is None
