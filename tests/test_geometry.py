"""
Tests for ptiles.geometry.

Pure computation, so these need no data files and always run.
"""

from __future__ import annotations


import pytest

from ptiles.geometry import (
    METERS_PER_DEG_LAT,
    angular_difference,
    bearing_degrees,
    haversine_meters,
    meters_per_deg_lon,
    point_in_polygon,
    point_to_linestring_distance_meters,
    point_to_polygon_distance_meters,
    point_to_ring_distance_meters,
    segment_crosses_ring,
    segments_intersect,
    within_bbox_meters,
)

# A ~111 m square, near the equator so degrees and metres stay legible.
SQUARE = ((0.0, 0.0), (0.001, 0.0), (0.001, 0.001), (0.0, 0.001))


class TestScaling:

    def test_lon_shrinks_with_latitude(self):
        assert meters_per_deg_lon(0.0) == pytest.approx(111_320.0, rel=1e-6)
        # cos(60 deg) == 0.5
        assert meters_per_deg_lon(60.0) == pytest.approx(55_660.0, rel=1e-3)

    def test_lon_scaling_agrees_with_haversine(self):
        """The planar approximation must track the sphere at city scale.

        This is the bug the old buildings-only copy had: it scaled longitude
        by the latitude constant, overstating east-west distance by 1/cos(lat).
        """
        for lat in (0.0, 39.0, 51.5, 64.0):
            planar = meters_per_deg_lon(lat)
            spherical = haversine_meters(lat, 0.0, lat, 1.0)
            assert planar == pytest.approx(spherical, rel=0.002)

    def test_pole_does_not_divide_by_zero(self):
        assert meters_per_deg_lon(90.0) > 0


class TestBearing:

    def test_cardinals(self):
        assert bearing_degrees(0, 0, 1, 0) == pytest.approx(0.0, abs=1e-6)
        assert bearing_degrees(0, 0, 0, 1) == pytest.approx(90.0, abs=1e-6)
        assert bearing_degrees(1, 0, 0, 0) == pytest.approx(180.0, abs=1e-6)
        assert bearing_degrees(0, 1, 0, 0) == pytest.approx(270.0, abs=1e-6)

    def test_always_in_range(self):
        for dlat, dlon in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            b = bearing_degrees(40.0, -80.0, 40.0 + dlat, -80.0 + dlon)
            assert 0.0 <= b < 360.0

    def test_angular_difference_wraps(self):
        assert angular_difference(10, 350) == pytest.approx(20.0)
        assert angular_difference(350, 10) == pytest.approx(20.0)
        assert angular_difference(0, 180) == pytest.approx(180.0)
        assert angular_difference(90, 90) == pytest.approx(0.0)


class TestPolygon:

    def test_containment(self):
        assert point_in_polygon(0.0005, 0.0005, SQUARE)
        assert not point_in_polygon(0.002, 0.0005, SQUARE)

    def test_degenerate_ring_is_not_containing(self):
        assert not point_in_polygon(0.0, 0.0, ((0.0, 0.0), (1.0, 1.0)))

    def test_distance_is_zero_inside(self):
        assert point_to_polygon_distance_meters(0.0005, 0.0005, SQUARE) == 0.0

    def test_ring_distance_ignores_containment(self):
        """Distance to the boundary is nonzero even from inside."""
        d = point_to_ring_distance_meters(0.0005, 0.0005, SQUARE)
        assert d == pytest.approx(55.66, rel=0.02)

    def test_distance_outside(self):
        # 0.001 deg east of the eastern edge, at the equator.
        d = point_to_polygon_distance_meters(0.002, 0.0005, SQUARE)
        assert d == pytest.approx(111.32, rel=0.01)

    def test_closes_the_ring(self):
        """The last-to-first edge must be measured, not skipped."""
        d = point_to_polygon_distance_meters(-0.0005, 0.0005, SQUARE)
        assert d == pytest.approx(55.66, rel=0.02)


class TestLinestring:

    def test_perpendicular_distance(self):
        line = ((0.0, 0.0), (0.002, 0.0))
        d, slon, slat, seg, t = point_to_linestring_distance_meters(0.001, 0.001, line)
        assert d == pytest.approx(METERS_PER_DEG_LAT * 0.001, rel=0.01)
        assert slon == pytest.approx(0.001, abs=1e-6)
        assert seg == 0

    def test_clamps_past_the_end(self):
        line = ((0.0, 0.0), (0.001, 0.0))
        d, _, _, _, t = point_to_linestring_distance_meters(0.003, 0.0, line)
        assert t == pytest.approx(1.0)
        assert d == pytest.approx(222.6, rel=0.02)

    def test_single_vertex_is_point_distance(self):
        d, _, _, _, _ = point_to_linestring_distance_meters(0.0, 0.001, ((0.0, 0.0),))
        assert d == pytest.approx(111.32, rel=0.01)

    def test_empty(self):
        d, _, _, _, _ = point_to_linestring_distance_meters(0.0, 0.0, ())
        assert d == float("inf")


class TestIntersection:

    def test_crossing(self):
        assert segments_intersect((0, 0), (1, 1), (0, 1), (1, 0))

    def test_parallel(self):
        assert not segments_intersect((0, 0), (1, 0), (0, 1), (1, 1))

    def test_shared_endpoint_is_not_a_crossing(self):
        """A sightline grazing a corner must not read as blocked."""
        assert not segments_intersect((0, 0), (1, 1), (1, 1), (2, 0))

    def test_segment_through_ring(self):
        assert segment_crosses_ring((-0.001, 0.0005), (0.002, 0.0005), SQUARE)

    def test_segment_clear_of_ring(self):
        assert not segment_crosses_ring((0.005, 0.0), (0.006, 0.001), SQUARE)


class TestBboxReject:

    def test_accepts_what_is_in_range(self):
        assert within_bbox_meters(0.0005, 0.0005, SQUARE, 1.0)
        assert within_bbox_meters(0.002, 0.0005, SQUARE, 200.0)

    def test_rejects_what_is_far(self):
        assert not within_bbox_meters(1.0, 1.0, SQUARE, 100.0)

    def test_never_rejects_something_actually_in_range(self):
        """The reject must be conservative or `within` silently loses buildings."""
        for lon in (-0.003, -0.001, 0.0005, 0.002, 0.004):
            for lat in (-0.003, 0.0005, 0.004):
                exact = point_to_polygon_distance_meters(lon, lat, SQUARE)
                for radius in (10.0, 50.0, 200.0, 500.0):
                    if exact <= radius:
                        assert within_bbox_meters(lon, lat, SQUARE, radius), (
                            f"bbox rejected a polygon {exact:.1f} m away "
                            f"at radius {radius}"
                        )

    def test_empty_polygon(self):
        assert not within_bbox_meters(0.0, 0.0, (), 100.0)
