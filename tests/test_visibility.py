"""
Tests for ptiles.visibility.

Uses a stub camera reader so the geometry is tested without depending on
US.camera.ptiles being present. The confidence tiers matter as much as the
yes/no: with a field of view on 0.2% of devices, a bare boolean would imply
precision the data does not have.
"""

from __future__ import annotations

import math

import pytest

from ptiles.camera import Camera
from ptiles.visibility import (
    DEFAULT_FOV_DEG,
    DEFAULT_RANGE_M,
    Confidence,
    cameras_near,
    cameras_seeing,
)

ORIGIN_LAT, ORIGIN_LON = 40.0, -75.0


def offset(lat: float, lon: float, bearing_deg: float, meters: float):
    """Move a point `meters` along a bearing."""
    R = 6_371_000.0
    br = math.radians(bearing_deg)
    dlat = (meters * math.cos(br)) / R
    dlon = (meters * math.sin(br)) / (R * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


class StubReader:
    """Minimal get_in_cell provider — nearest() needs nothing more."""

    def __init__(self, cameras):
        import h3
        self._by_cell = {}
        for c in cameras:
            cell = int(h3.latlng_to_cell(c.lat, c.lon, 7), 16)
            self._by_cell.setdefault(cell, []).append(c)

    def get_in_cell(self, cell):
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        return self._by_cell.get(cell_int, [])


def make_camera(bearing, *, camera_type="fixed", device_type="camera",
                angle=None, osm_id=1, at=None):
    lat, lon = at if at else (ORIGIN_LAT, ORIGIN_LON)
    return Camera(osm_id=osm_id, lat=lat, lon=lon, device_type=device_type,
                  placement="outdoor", camera_type=camera_type,
                  direction=bearing, angle=angle)


class TestBearingTest:

    def test_point_in_front_is_seen(self):
        cam = make_camera(90)                      # pointing east
        reader = StubReader([cam])
        pt = offset(ORIGIN_LAT, ORIGIN_LON, 90, 20)
        hits = cameras_seeing(*pt, reader)
        assert len(hits) == 1
        assert hits[0].off_axis_degrees == pytest.approx(0.0, abs=0.5)

    def test_point_behind_is_not_seen(self):
        cam = make_camera(90)
        reader = StubReader([cam])
        pt = offset(ORIGIN_LAT, ORIGIN_LON, 270, 20)
        assert cameras_seeing(*pt, reader) == []

    def test_edge_of_cone(self):
        """A 60 deg default cone reaches 30 deg either side and no further."""
        cam = make_camera(90, camera_type="fixed")
        reader = StubReader([cam])
        assert cameras_seeing(*offset(ORIGIN_LAT, ORIGIN_LON, 115, 20), reader)
        assert not cameras_seeing(*offset(ORIGIN_LAT, ORIGIN_LON, 135, 20), reader)

    def test_stated_angle_widens_the_cone(self):
        cam = make_camera(90, angle=180)
        reader = StubReader([cam])
        hits = cameras_seeing(*offset(ORIGIN_LAT, ORIGIN_LON, 170, 20), reader)
        assert hits and hits[0].confidence is Confidence.CERTAIN


class TestOmnidirectional:

    @pytest.mark.parametrize("camera_type", ["dome", "panning"])
    @pytest.mark.parametrize("bearing", [0, 90, 180, 270])
    def test_sweeping_units_see_every_direction(self, camera_type, bearing):
        cam = make_camera(90, camera_type=camera_type)
        reader = StubReader([cam])
        hits = cameras_seeing(*offset(ORIGIN_LAT, ORIGIN_LON, bearing, 20), reader)
        assert len(hits) == 1
        assert hits[0].confidence is Confidence.OMNIDIRECTIONAL

    def test_no_bearing_is_omnidirectional(self):
        cam = make_camera(None)
        reader = StubReader([cam])
        hits = cameras_seeing(*offset(ORIGIN_LAT, ORIGIN_LON, 200, 20), reader)
        assert len(hits) == 1
        assert hits[0].confidence is Confidence.OMNIDIRECTIONAL
        assert hits[0].off_axis_degrees is None


class TestRange:

    def test_beyond_assumed_range_is_dropped(self):
        cam = make_camera(90, device_type="ALPR")     # 50 m default
        reader = StubReader([cam])
        near = offset(ORIGIN_LAT, ORIGIN_LON, 90, 30)
        far = offset(ORIGIN_LAT, ORIGIN_LON, 90, 120)
        assert cameras_seeing(*near, reader)
        assert not cameras_seeing(*far, reader)

    def test_range_override(self):
        cam = make_camera(90, device_type="ALPR")
        reader = StubReader([cam])
        pt = offset(ORIGIN_LAT, ORIGIN_LON, 90, 120)
        assert cameras_seeing(*pt, reader, range_overrides={"ALPR": 200.0})

    def test_assumptions_are_reported(self):
        """A caller must be able to see what was assumed, not just the verdict."""
        cam = make_camera(90, device_type="ALPR", camera_type="fixed")
        reader = StubReader([cam])
        hit = cameras_seeing(*offset(ORIGIN_LAT, ORIGIN_LON, 90, 20), reader)[0]
        assert hit.assumed_range_m == DEFAULT_RANGE_M["ALPR"]
        assert hit.assumed_fov_deg == DEFAULT_FOV_DEG["fixed"]
        assert hit.confidence is Confidence.ASSUMED


class TestOrdering:

    def test_nearest_first(self):
        cams = [
            make_camera(90, osm_id=1, at=offset(ORIGIN_LAT, ORIGIN_LON, 270, 60)),
            make_camera(90, osm_id=2, at=offset(ORIGIN_LAT, ORIGIN_LON, 270, 20)),
            make_camera(90, osm_id=3, at=offset(ORIGIN_LAT, ORIGIN_LON, 270, 40)),
        ]
        reader = StubReader(cams)
        hits = cameras_seeing(ORIGIN_LAT, ORIGIN_LON, reader)
        assert [h.camera.osm_id for h in hits] == [2, 3, 1]
        assert hits == sorted(hits, key=lambda h: h.distance_meters)


class TestCamerasNear:

    def test_includes_cameras_pointing_away(self):
        """`near` answers 'what is around me', not 'what sees me'."""
        cam = make_camera(90)
        reader = StubReader([cam])
        pt = offset(ORIGIN_LAT, ORIGIN_LON, 270, 20)
        assert cameras_seeing(*pt, reader) == []
        assert len(cameras_near(*pt, reader, max_meters=100)) == 1
