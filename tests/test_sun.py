"""
Tests for ptiles.sun.

The solar position is checked against values reproduced from pvlib's NREL
implementation (agreement was 0.01 deg across these cases when written), plus
astronomical identities that hold independently of any implementation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ptiles.sun import (
    ShadeResult,
    SunPosition,
    shadow_length_meters,
    sun_position,
)

# (label, lat, lon, utc, azimuth, altitude) — reference values from pvlib
# solarposition.get_solarposition(..., method="nrel_numpy"), non-refracted
# elevation.
NREL_CASES = [
    ("Boulder equinox", 40.0150, -105.2705, datetime(2024, 3, 20, 16, 0), 120.7, 31.6),
    ("Boulder solstice", 40.0150, -105.2705, datetime(2024, 6, 21, 18, 0), 136.6, 68.7),
    ("NYC equinox", 40.7128, -74.0060, datetime(2024, 3, 20, 16, 0), 156.4, 47.0),
    ("NYC solstice", 40.7128, -74.0060, datetime(2024, 6, 21, 18, 0), 221.7, 68.4),
    ("Sydney (south)", -33.8688, 151.2093, datetime(2024, 9, 22, 6, 0), 285.9, 22.0),
    ("Reykjavik (high lat)", 64.1466, -21.9426, datetime(2024, 6, 21, 18, 0), 260.5, 30.7),
    ("Singapore (equator)", 1.3521, 103.8198, datetime(2024, 9, 22, 6, 0), 265.6, 74.3),
    ("night", 40.7128, -74.0060, datetime(2024, 9, 22, 6, 0), 26.2, -46.1),
]


class TestSunPosition:

    @pytest.mark.parametrize("label,lat,lon,utc,az,alt", NREL_CASES,
                             ids=[c[0] for c in NREL_CASES])
    def test_matches_nrel(self, label, lat, lon, utc, az, alt):
        p = sun_position(lat, lon, utc.replace(tzinfo=timezone.utc))
        assert p.altitude == pytest.approx(alt, abs=0.1)
        delta = abs((p.azimuth - az + 180) % 360 - 180)
        assert delta < 0.15, f"azimuth {p.azimuth} vs {az}"

    def test_azimuth_always_in_range(self):
        base = datetime(2024, 5, 1, tzinfo=timezone.utc)
        for minutes in range(0, 1440, 37):
            p = sun_position(51.5, -0.13, base + timedelta(minutes=minutes))
            assert 0.0 <= p.azimuth < 360.0
            assert -90.0 <= p.altitude <= 90.0

    def test_naive_datetime_is_treated_as_utc(self):
        naive = datetime(2024, 6, 21, 18, 0)
        aware = naive.replace(tzinfo=timezone.utc)
        assert sun_position(40.0, -105.0, naive).altitude == pytest.approx(
            sun_position(40.0, -105.0, aware).altitude
        )

    def test_solstice_noon_altitude_matches_geometry(self):
        """Peak altitude at the June solstice is 90 - lat + obliquity."""
        for lat in (25.0, 40.0, 55.0):
            peak = max(
                sun_position(lat, 0.0,
                             datetime(2024, 6, 21, tzinfo=timezone.utc)
                             + timedelta(minutes=m)).altitude
                for m in range(0, 1440, 2)
            )
            assert peak == pytest.approx(90.0 - lat + 23.44, abs=0.2)

    def test_solar_noon_faces_the_equator(self):
        """Sun is due south at noon in the north, due north in the south."""
        day = datetime(2024, 6, 21, tzinfo=timezone.utc)
        north = max((sun_position(40.0, 0.0, day + timedelta(minutes=m))
                     for m in range(0, 1440, 2)), key=lambda p: p.altitude)
        assert north.azimuth == pytest.approx(180.0, abs=1.0)

        south = max((sun_position(-33.9, 0.0, day + timedelta(minutes=m))
                     for m in range(0, 1440, 2)), key=lambda p: p.altitude)
        assert south.azimuth == pytest.approx(0.0, abs=1.0) or \
            south.azimuth == pytest.approx(360.0, abs=1.0)

    def test_equinox_sunrise_is_due_east(self):
        """At the equinox the sun crosses the horizon near due east/west."""
        day = datetime(2024, 3, 20, tzinfo=timezone.utc)
        samples = [sun_position(0.0, 0.0, day + timedelta(minutes=m))
                   for m in range(0, 1440)]
        rising = min((p for p in samples if -0.5 < p.altitude < 0.5
                      and p.azimuth < 180),
                     key=lambda p: abs(p.altitude))
        assert rising.azimuth == pytest.approx(90.0, abs=1.5)

    def test_is_up(self):
        assert SunPosition(180.0, 10.0).is_up
        assert not SunPosition(180.0, -0.5).is_up


class TestShadowLength:

    def test_45_degrees_equals_height(self):
        assert shadow_length_meters(10.0, 45.0) == pytest.approx(10.0)

    def test_grows_as_sun_lowers(self):
        lengths = [shadow_length_meters(10.0, a) for a in (60, 30, 15, 5)]
        assert lengths == sorted(lengths)

    def test_sun_below_horizon_is_infinite(self):
        assert shadow_length_meters(10.0, 0.0) == float("inf")
        assert shadow_length_meters(10.0, -5.0) == float("inf")


class TestShadeResult:

    def test_unknown_is_not_confident(self):
        r = ShadeResult(None, SunPosition(180.0, 30.0), 12, 0)
        assert r.shaded is None
        assert not r.is_confident

    def test_false_is_confident(self):
        """Sunlit is a real answer; unknown is not. They must not collapse."""
        r = ShadeResult(False, SunPosition(180.0, 30.0), 12, 4)
        assert r.is_confident
        assert r.shaded is False
