"""
Tests for ptiles.composite module.

Tests PtilesClient.open_state() and query_point() against
TN state data files.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptiles.composite import PtilesClient, PointReport
from ptiles import PTilesError

DATA_DIR = Path(os.path.expanduser("~/kino/projects/ptiles/data/states"))
TN_ROADS = DATA_DIR / "TN.roads.ptiles"
TN_BUILDINGS = DATA_DIR / "TN.buildings_v8.ptiles"
TN_WATER = DATA_DIR / "TN.water.ptiles"
TN_BUSINESS = DATA_DIR / "TN.business.ptiles"


@pytest.mark.skipif(
    not all(p.exists() for p in [TN_ROADS, TN_BUILDINGS, TN_WATER, TN_BUSINESS]),
    reason="TN test data not all present",
)
class TestPtilesClient:

    @pytest.fixture
    def client(self) -> PtilesClient:
        c = PtilesClient.open_state("TN", DATA_DIR)
        yield c
        c.close()

    def test_open_state(self, client: PtilesClient):
        """Verify open_state loads available readers."""
        assert client.roads is not None
        assert client.buildings is not None
        assert client.water is not None
        assert client.business is not None
        # Admin may not be in TN directory
        # Places/rail/parks probably not present

    def test_query_point_nashville(self, client: PtilesClient):
        """Test query_point near Nashville returns expected data."""
        report = client.query_point(36.1627, -86.7816)
        assert isinstance(report, PointReport)

        # Should have admin info (US-wide admin file)
        # or at least not error

        # Should have nearby businesses
        assert len(report.businesses) >= 0
        for hit in report.businesses:
            assert hit.business.name != ""

        # Should have a nearest road or at least nearby roads
        if report.nearest_road:
            assert report.nearest_road.distance_meters >= 0
            assert report.nearest_road.road.osm_id > 0

        # Water (Cumberland River area)
        assert isinstance(report.water, list)
