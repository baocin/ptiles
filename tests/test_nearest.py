"""
Tests for ptiles.nearest.

The interesting property is the early exit: finding `n` matches is not a
reason to stop, because a match in ring k can be farther than one in ring k+1.
These tests check the spiral against brute force over a wide disk, which is
the only way to catch a bound that is too tight.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import h3
import pytest

from ptiles.geometry import haversine_meters
from ptiles.nearest import (
    NearestResult,
    feature_distance_meters,
    match_attr,
    match_name,
    nearest,
)

H3_RES = 7


@dataclass(frozen=True)
class Point:
    osm_id: int
    lat: float
    lon: float
    name: str | None = None
    brand: str | None = None
    kind: str = "generic"


@dataclass(frozen=True)
class Line:
    osm_id: int
    coords: tuple
    name: str | None = None


@dataclass(frozen=True)
class Area:
    osm_id: int
    coordinates: tuple
    name: str | None = None


class StubReader:
    def __init__(self, features):
        self._by_cell = {}
        self.cells_read = 0
        for f in features:
            lat = getattr(f, "lat", None)
            lon = getattr(f, "lon", None)
            if lat is None:
                coords = getattr(f, "coords", None) or getattr(f, "coordinates")
                lon, lat = coords[0]
            cell = int(h3.latlng_to_cell(lat, lon, H3_RES), 16)
            self._by_cell.setdefault(cell, []).append(f)

    def get_in_cell(self, cell):
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        self.cells_read += 1
        return self._by_cell.get(cell_int, [])


def scatter(seed, count, lat0=39.0, lon0=-77.0, spread=0.35):
    rng = random.Random(seed)
    return [
        Point(
            osm_id=i,
            lat=lat0 + (rng.random() - 0.5) * spread,
            lon=lon0 + (rng.random() - 0.5) * spread,
            name=rng.choice(["Taco Bell", "Taco Bell Cantina", "Tacos R Us",
                             "Starbucks", "Bell Labs", None]),
            kind=rng.choice(["river", "stream", "lake"]),
        )
        for i in range(count)
    ]


class TestSpiralCorrectness:
    """The spiral must agree with brute force, or the early exit is wrong."""

    @pytest.mark.parametrize("n", [1, 3, 5, 10])
    def test_matches_brute_force(self, n):
        features = scatter(seed=1, count=1200)
        reader = StubReader(features)
        rng = random.Random(99)

        for _ in range(25):
            lat = 39.0 + (rng.random() - 0.5) * 0.2
            lon = -77.0 + (rng.random() - 0.5) * 0.2

            got = [round(h.distance_meters, 6) for h in nearest(reader, lat, lon, n=n)]
            want = sorted(
                round(haversine_meters(lat, lon, f.lat, f.lon), 6) for f in features
            )[:n]
            assert got == want, f"spiral disagreed with brute force at {lat},{lon}"

    def test_matches_brute_force_with_predicate(self):
        features = scatter(seed=2, count=1500)
        reader = StubReader(features)
        pred = match_name("Taco Bell")
        rng = random.Random(7)

        for _ in range(15):
            lat = 39.0 + (rng.random() - 0.5) * 0.2
            lon = -77.0 + (rng.random() - 0.5) * 0.2
            got = [round(h.distance_meters, 6)
                   for h in nearest(reader, lat, lon, predicate=pred, n=3)]
            want = sorted(
                round(haversine_meters(lat, lon, f.lat, f.lon), 6)
                for f in features if pred(f)
            )[:3]
            assert got == want

    def test_stops_early_when_dense(self):
        """A hit in the home cell should not trigger a wide scan."""
        reader = StubReader(scatter(seed=3, count=4000))
        result = nearest(reader, 39.0, -77.0, n=1)
        assert result.rings_scanned <= 2
        assert result.cells_read < 25


class TestLimits:

    def test_max_meters_bounds_the_spiral(self):
        reader = StubReader(scatter(seed=4, count=800))
        result = nearest(reader, 39.0, -77.0, n=50, max_meters=300)
        assert all(h.distance_meters <= 300 for h in result)
        assert result.rings_scanned <= 2

    def test_exhausted_when_nothing_matches(self):
        reader = StubReader(scatter(seed=5, count=500))
        result = nearest(reader, 39.0, -77.0,
                         predicate=match_name("Nonexistent Chain"), n=1)
        assert len(result) == 0
        assert result.exhausted

    def test_exhausted_when_fewer_than_n(self):
        reader = StubReader([Point(1, 39.0, -77.0, name="Only One")])
        result = nearest(reader, 39.0, -77.0, n=5)
        assert len(result) == 1
        assert result.exhausted

    def test_max_rings_is_respected(self):
        reader = StubReader(scatter(seed=6, count=200))
        result = nearest(reader, 39.0, -77.0,
                         predicate=match_name("Nonexistent"), n=1, max_rings=2)
        assert result.rings_scanned <= 2

    def test_rejects_zero_n(self):
        with pytest.raises(ValueError):
            nearest(StubReader([]), 39.0, -77.0, n=0)


class TestResultProtocol:

    def test_iterates_and_indexes_as_hits(self):
        reader = StubReader(scatter(seed=8, count=300))
        result = nearest(reader, 39.0, -77.0, n=3)
        assert isinstance(result, NearestResult)
        assert len(result) == 3
        assert list(result) == result.hits
        assert result[0] is result.hits[0]
        assert bool(result)

    def test_empty_is_falsy(self):
        result = nearest(StubReader([]), 39.0, -77.0, n=1)
        assert not result


class TestPredicates:

    def test_match_name_is_prefix_and_case_insensitive(self):
        p = match_name("taco bell")
        assert p(Point(1, 0, 0, name="Taco Bell"))
        assert p(Point(1, 0, 0, name="Taco Bell Cantina"))
        assert not p(Point(1, 0, 0, name="Tacos R Us"))
        assert not p(Point(1, 0, 0, name=None))

    def test_match_name_exact(self):
        p = match_name("Taco Bell", exact=True)
        assert p(Point(1, 0, 0, name="Taco Bell"))
        assert not p(Point(1, 0, 0, name="Taco Bell Cantina"))

    def test_match_name_checks_brand(self):
        p = match_name("Starbucks")
        assert p(Point(1, 0, 0, name="Coffee Shop", brand="Starbucks"))

    def test_match_attr(self):
        p = match_attr("kind", "river", "stream")
        assert p(Point(1, 0, 0, kind="river"))
        assert p(Point(1, 0, 0, kind="stream"))
        assert not p(Point(1, 0, 0, kind="lake"))


class TestGeometryDispatch:
    """A lake's centroid can be far from its shore; measure to the geometry."""

    def test_point_uses_haversine(self):
        d = feature_distance_meters(39.0, -77.0, Point(1, 39.001, -77.0))
        assert d == pytest.approx(111.3, rel=0.02)

    def test_line_measures_to_the_segment(self):
        line = Line(1, ((-77.001, 39.0), (-76.999, 39.0)))
        # Directly above the middle of the line, not near either endpoint.
        d = feature_distance_meters(39.0005, -77.0, line)
        assert d == pytest.approx(55.7, rel=0.05)

    def test_area_is_zero_inside(self):
        ring = ((-77.001, 38.999), (-76.999, 38.999),
                (-76.999, 39.001), (-77.001, 39.001))
        assert feature_distance_meters(39.0, -77.0, Area(1, ring)) == 0.0

    def test_area_measured_from_outside(self):
        ring = ((-77.001, 38.999), (-76.999, 38.999),
                (-76.999, 39.001), (-77.001, 39.001))
        d = feature_distance_meters(39.0, -76.998, Area(1, ring))
        assert d == pytest.approx(86.5, rel=0.05)

    def test_unknown_geometry_is_infinite(self):
        @dataclass(frozen=True)
        class Opaque:
            osm_id: int

        assert feature_distance_meters(39.0, -77.0, Opaque(1)) == float("inf")
