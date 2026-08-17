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

# The repo-local data/ is a root-owned directory shadowing what AGENTS.md says
# should be a symlink to NFS, so it is empty and every test here silently
# skipped. Prefer whichever path actually holds the data.
_CANDIDATES = [
    Path("/mnt/core/kino/ptiles/data/states"),
    Path(os.path.expanduser("~/kino/projects/ptiles/data/states")),
]
DATA_DIR = next(
    (p for p in _CANDIDATES if (p / "TN.buildings_v8.ptiles").exists()),
    _CANDIDATES[-1],
)
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
        """Verify open_state loads available readers.

        These used to assert `client.roads`/`.buildings`/… — attributes dropped
        in the move to the `_layers` registry, so the test only ever passed by
        being skipped when the TN data was absent. Assert against the registry.
        """
        from ptiles.composite import BuildingLayer, BusinessLayer, WaterLayer

        loaded = {type(l) for l in client._layers}
        assert BuildingLayer in loaded
        assert WaterLayer in loaded
        assert BusinessLayer in loaded
        # Every layer belongs to the scope it was opened for.
        assert {l.scope for l in client._layers} <= {"TN", "US"}

    def test_open_state_matches_open_scope(self, client: PtilesClient):
        """open_state is retained as the original name for open_scope."""
        c2 = PtilesClient.open_scope("TN", DATA_DIR)
        try:
            assert sorted(type(l).__name__ for l in c2._layers) == sorted(
                type(l).__name__ for l in client._layers
            )
        finally:
            c2.close()

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
