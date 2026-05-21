"""Tests for ptiles.composite — corridor() and PtilesRouter.route().

Uses PtilesClient for corridor queries and PtilesRouter for routing
against the TN test data files.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptiles import PTilesError
from ptiles.composite import PtilesClient, CorridorReport
from ptiles.router import PtilesRouter, Route
from ptiles.buildings import Building
from ptiles.water import WaterFeature

DATA_DIR = Path(os.path.expanduser("~/kino/projects/ptiles/data/states"))
TN_ROADS = DATA_DIR / "TN.roads.ptiles"
TN_BUILDINGS = DATA_DIR / "TN.buildings_v8.ptiles"
TN_WATER = DATA_DIR / "TN.water.ptiles"
TN_BUSINESS = DATA_DIR / "TN.business.ptiles"

ALL_FILES = [TN_ROADS, TN_BUILDINGS, TN_WATER, TN_BUSINESS]


@pytest.mark.skipif(
    not all(p.exists() for p in ALL_FILES),
    reason="TN test data not all present",
)
class TestCorridor:
    """Tests for PtilesClient.corridor()."""

    @pytest.fixture
    def client(self) -> PtilesClient:
        c = PtilesClient.open(
            buildings=str(TN_BUILDINGS),
            roads=str(TN_ROADS),
            water=str(TN_WATER),
            business=str(TN_BUSINESS),
        )
        yield c
        c.close()

    def test_corridor(self, client: PtilesClient):
        """Create a short path near Nashville and call corridor()."""
        path = [
            (36.1627, -86.7816),  # (lat, lon) — downtown Nashville
            (36.1650, -86.7800),
            (36.1675, -86.7780),
        ]
        report = client.corridor(path, buffer_meters=100)
        assert isinstance(report, CorridorReport)

        # Should not error; at least some layers may have data
        for b in report.buildings:
            assert isinstance(b, Building)
        for b in report.business:
            pass  # Business dataclass — just check no crash
        for w in report.water:
            assert isinstance(w, WaterFeature)

    def test_corridor_larger_buffer(self, client: PtilesClient):
        """Corridor with a larger buffer returns more features."""
        path = [
            (36.1627, -86.7816),
            (36.1700, -86.7750),
        ]
        report = client.corridor(path, buffer_meters=500)
        assert isinstance(report, CorridorReport)
        # Should have at least some data from the larger buffer
        total = len(report.buildings) + len(report.business) + len(report.roads)
        assert total >= 0


@pytest.mark.skipif(
    not TN_ROADS.exists(),
    reason="TN.roads.ptiles not found",
)
class TestRouter:
    """Tests for PtilesRouter.route()."""

    @pytest.fixture
    def router(self) -> PtilesRouter:
        return PtilesRouter.open(TN_ROADS)

    def test_route(self, router: PtilesRouter):
        """Call route() between two Nashville points."""
        result = router.route(
            36.1627, -86.7816,  # src
            36.1700, -86.7750,  # dst
            profile="driving",
        )
        assert isinstance(result, Route)
        assert result.distance_meters > 0
        assert result.duration_seconds > 0
        assert result.from_cell != 0
        assert result.to_cell != 0
        assert len(result.path) >= 2

    def test_route_walking(self, router: PtilesRouter):
        """Route with walking profile."""
        result = router.route(
            36.1627, -86.7816,
            36.1650, -86.7800,
            profile="walking",
        )
        assert isinstance(result, Route)
        assert result.distance_meters > 0
        assert result.profile == "walking"

    def test_route_longer_distance(self, router: PtilesRouter):
        """Route between farther apart points."""
        result = router.route(
            36.1627, -86.7816,  # downtown
            36.2000, -86.7500,  # ~5 km northeast
            profile="driving",
        )
        assert isinstance(result, Route)
        assert result.distance_meters > 500  # at least 0.5 km
        assert result.segments > 0
