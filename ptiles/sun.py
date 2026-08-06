"""
Sun position, and whether a point is in a building's shadow.

Pure computation over data already in the files: footprints from the buildings
layer, local time from the admin layer's per-cell timezone. Nothing new to
download.

**Coverage is the limiting factor, not the maths.** Building height reaches
0.3% of footprints in the shipped v9 files (503 of 161,593 in DC, which is
among the better-mapped US cities), because the build pipeline reads only the
OSM `height` tag. So `is_shaded` returns None — unknown — far more often than
it returns True or False, and it reports how many candidate buildings actually
had a height so a caller can tell "you are in the sun" from "we cannot say".
Treating None as False would render most of a city as sunlit on no evidence.

Shadows are computed for flat roofs at the stated height. Roof shape, terrain,
and vegetation are not modelled.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from ptiles.geometry import METERS_PER_DEG_LAT, meters_per_deg_lon

logger = logging.getLogger("ptiles.sun")


@dataclass(frozen=True, slots=True)
class SunPosition:
    azimuth: float
    """Compass bearing of the sun, degrees clockwise from north."""
    altitude: float
    """Degrees above the horizon. Negative when the sun is down."""

    @property
    def is_up(self) -> bool:
        return self.altitude > 0.0


@dataclass(frozen=True, slots=True)
class ShadeResult:
    """Whether a point is shaded, and how much to trust that.

    `shaded` is None when the question could not be answered from the data —
    either the sun is down, or no nearby building carried a height. Callers
    must distinguish that from False.
    """

    shaded: bool | None
    sun: SunPosition
    buildings_considered: int
    buildings_with_height: int
    shaded_by: int | None = None
    """OSM id of the building casting the shadow, when shaded."""
    reason: str = ""

    @property
    def is_confident(self) -> bool:
        return self.shaded is not None


def _julian_day(dt: datetime) -> float:
    """Julian day number from a UTC datetime."""
    y, m = dt.year, dt.month
    d = (dt.day + dt.hour / 24.0 + dt.minute / 1440.0
         + (dt.second + dt.microsecond / 1e6) / 86400.0)
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1))
            + d + b - 1524.5)


def sun_position(lat: float, lon: float, when: datetime) -> SunPosition:
    """Solar azimuth and altitude, via the NOAA solar position algorithm.

    `when` must be timezone-aware; a naive datetime is read as UTC, which is
    almost never what a caller means — use `local_now` or attach a tzinfo.
    Accuracy is ~0.01 deg, well inside what shadow work needs.
    """
    if when.tzinfo is None:
        logger.warning("naive datetime passed to sun_position; assuming UTC")
        when = when.replace(tzinfo=timezone.utc)
    utc = when.astimezone(timezone.utc)

    jd = _julian_day(utc)
    t = (jd - 2451545.0) / 36525.0                      # Julian century

    # Geometric mean longitude and anomaly of the sun.
    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    m_rad = math.radians(mean_anom)
    center = (math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
              + math.sin(3 * m_rad) * 0.000289)

    true_long = mean_long + center
    omega = 125.04 - 1934.136 * t
    apparent_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    mean_obliq = (23.0 + (26.0 + ((21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))))
                          / 60.0) / 60.0)
    obliq_corr = mean_obliq + 0.00256 * math.cos(math.radians(omega))

    declination = math.degrees(math.asin(
        math.sin(math.radians(obliq_corr)) * math.sin(math.radians(apparent_long))
    ))

    # Equation of time, in minutes.
    y = math.tan(math.radians(obliq_corr / 2.0)) ** 2
    l0_rad = math.radians(mean_long)
    eq_time = 4.0 * math.degrees(
        y * math.sin(2 * l0_rad)
        - 2 * eccentricity * math.sin(m_rad)
        + 4 * eccentricity * y * math.sin(m_rad) * math.cos(2 * l0_rad)
        - 0.5 * y * y * math.sin(4 * l0_rad)
        - 1.25 * eccentricity * eccentricity * math.sin(2 * m_rad)
    )

    minutes_utc = (utc.hour * 60.0 + utc.minute
                   + (utc.second + utc.microsecond / 1e6) / 60.0)
    true_solar_time = (minutes_utc + eq_time + 4.0 * lon) % 1440.0

    hour_angle = true_solar_time / 4.0 - 180.0
    if hour_angle < -180.0:
        hour_angle += 360.0

    lat_rad = math.radians(lat)
    dec_rad = math.radians(declination)
    ha_rad = math.radians(hour_angle)

    cos_zenith = (math.sin(lat_rad) * math.sin(dec_rad)
                  + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad))
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.degrees(math.acos(cos_zenith))
    altitude = 90.0 - zenith

    sin_zenith = math.sin(math.radians(zenith))
    if abs(sin_zenith) < 1e-9:
        azimuth = 180.0
    else:
        # acos here yields the angle measured from due south; NOAA folds it to
        # a north-based compass bearing by the hour-angle sign — before noon
        # the sun is east of south, after noon west of it.
        cos_az = ((math.sin(lat_rad) * cos_zenith - math.sin(dec_rad))
                  / (math.cos(lat_rad) * sin_zenith))
        cos_az = max(-1.0, min(1.0, cos_az))
        acos_az = math.degrees(math.acos(cos_az))
        if hour_angle > 0:
            azimuth = (acos_az + 180.0) % 360.0
        else:
            azimuth = (540.0 - acos_az) % 360.0

    return SunPosition(azimuth=azimuth % 360.0, altitude=altitude)


def local_time(lat: float, lon: float, admin_reader,
               when: datetime | None = None) -> datetime:
    """Resolve wall-clock local time at a coordinate.

    This is what the admin layer's per-cell `tz_idx` is for: it carries an IANA
    zone name for every land cell, so local time needs no network lookup and no
    timezone-polygon dependency. Falls back to UTC when the cell is outside
    coverage (ocean) or the zone name is unknown to the platform.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if when is None:
        when = datetime.now(timezone.utc)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    try:
        info = admin_reader.query(lat, lon)
    except Exception as e:
        logger.warning("admin lookup failed, using UTC: %s", e)
        return when

    if info is None or not info.timezone:
        logger.warning("no timezone for %.5f,%.5f (ocean or uncovered); using UTC",
                       lat, lon)
        return when

    try:
        return when.astimezone(ZoneInfo(info.timezone))
    except (ZoneInfoNotFoundError, ValueError) as e:
        logger.warning("timezone %r not available, using UTC: %s", info.timezone, e)
        return when


def shadow_length_meters(height_m: float, altitude_deg: float) -> float:
    """How far a flat roof of this height throws its shadow."""
    if altitude_deg <= 0.0:
        return float("inf")
    return height_m / math.tan(math.radians(altitude_deg))


def _ray_first_hit_meters(lat: float, lon: float, bearing_deg: float,
                          ring: tuple[tuple[float, float], ...]) -> float | None:
    """Distance to where a ray from (lat, lon) first crosses a footprint.

    Returns None when the ray misses. Works in a local metres frame so the
    result is a real distance rather than a degree measure.
    """
    mlon = meters_per_deg_lon(lat)
    br = math.radians(bearing_deg)
    dx, dy = math.sin(br), math.cos(br)          # east, north components

    best: float | None = None
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        # Edge endpoints relative to the query point, in metres.
        ex1 = (ax - lon) * mlon
        ey1 = (ay - lat) * METERS_PER_DEG_LAT
        ex2 = (bx - lon) * mlon
        ey2 = (by - lat) * METERS_PER_DEG_LAT

        sx, sy = ex2 - ex1, ey2 - ey1
        denom = dx * sy - dy * sx
        if abs(denom) < 1e-12:
            continue                               # parallel
        # Solve  P + t*d = E1 + u*s  for t along the ray, u along the edge.
        t = (ex1 * sy - ey1 * sx) / denom
        u = (ex1 * dy - ey1 * dx) / denom
        if t < 0.0 or not (0.0 <= u <= 1.0):
            continue
        if best is None or t < best:
            best = t
    return best


def is_shaded(lat: float, lon: float, buildings, when: datetime,
              *, search_meters: float = 200.0) -> ShadeResult:
    """Is this point in a building's shadow at this moment?

    The test is exact for flat roofs: walk from the point toward the sun, and
    if the first footprint the sightline crosses at horizontal distance d has
    height greater than d * tan(altitude), that building blocks the sun.

    Returns ShadeResult, whose `shaded` is None when the answer is unknown —
    the sun is down, or nothing nearby has a height. Do not read None as False.
    """
    sun = sun_position(lat, lon, when)

    if not sun.is_up:
        return ShadeResult(None, sun, 0, 0, reason="sun is below the horizon")

    try:
        candidates = buildings.within(lat, lon, search_meters)
    except Exception as e:
        logger.warning("building lookup failed: %s", e)
        return ShadeResult(None, sun, 0, 0, reason=f"building lookup failed: {e}")

    with_height = [b for b in candidates if b.height_m]
    if not with_height:
        return ShadeResult(
            None, sun, len(candidates), 0,
            reason=(f"none of the {len(candidates)} buildings within "
                    f"{search_meters:g} m has a height"),
        )

    tan_alt = math.tan(math.radians(sun.altitude))
    for bldg in with_height:
        hit = _ray_first_hit_meters(lat, lon, sun.azimuth, bldg.coordinates)
        if hit is None:
            continue
        if hit * tan_alt < bldg.height_m:
            return ShadeResult(
                True, sun, len(candidates), len(with_height),
                shaded_by=bldg.osm_id,
                reason=(f"{bldg.height_m:g} m building at {hit:.0f} m blocks a "
                        f"{sun.altitude:.1f} deg sun"),
            )

    return ShadeResult(
        False, sun, len(candidates), len(with_height),
        reason=(f"no height-bearing building blocks the sun "
                f"({len(with_height)} of {len(candidates)} had a height)"),
    )


def shadow_polygon(building, sun: SunPosition
                   ) -> tuple[tuple[float, float], ...]:
    """Ground shadow of a footprint, as a convex ring in (lon, lat).

    The hull of the footprint and its translation away from the sun. Convex, so
    a concave building's shadow is over-approximated — fine for rendering, and
    `is_shaded` does the exact test.
    """
    if not building.height_m or not sun.is_up:
        return ()
    ring = building.coordinates
    if len(ring) < 3:
        return ()

    length = shadow_length_meters(building.height_m, sun.altitude)
    away = math.radians((sun.azimuth + 180.0) % 360.0)
    mid_lat = sum(p[1] for p in ring) / len(ring)
    dlon = (length * math.sin(away)) / meters_per_deg_lon(mid_lat)
    dlat = (length * math.cos(away)) / METERS_PER_DEG_LAT

    points = list(ring) + [(x + dlon, y + dlat) for x, y in ring]
    return _convex_hull(points)


def _convex_hull(points: list[tuple[float, float]]
                 ) -> tuple[tuple[float, float], ...]:
    """Andrew's monotone chain. Returns a closed counter-clockwise ring."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return tuple(pts)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    hull = lower[:-1] + upper[:-1]
    return tuple(hull) + (hull[0],)
