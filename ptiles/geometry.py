"""
Shared planar/spherical geometry for PTiles readers.

Every layer needs the same handful of primitives — how far is this point from
that line, is it inside that ring, what bearing is it on. They had drifted into
three near-copies (roads, buildings, business), one of which scaled longitude
degrees as if the earth were flat at every latitude. This is the single copy.

Distances are metres. Coordinates are (lon, lat) degree pairs, matching the
order used throughout the decoders.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0
METERS_PER_DEG_LAT = 111_320.0


def meters_per_deg_lon(lat: float) -> float:
    """Metres per degree of longitude at a latitude.

    Clamped away from zero so a query at the pole doesn't divide by nothing.
    """
    return METERS_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.001)


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in metres."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return EARTH_RADIUS_M * 2 * math.asin(math.sqrt(a))


def bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, in [0, 360)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    return math.degrees(math.atan2(y, x)) % 360.0


def angular_difference(a_deg: float, b_deg: float) -> float:
    """Smallest absolute angle between two bearings, in [0, 180]."""
    return abs((a_deg - b_deg + 180.0) % 360.0 - 180.0)


def point_to_segment_distance_meters(
    px: float, py: float,          # query (lon, lat)
    ax: float, ay: float,          # segment start (lon, lat)
    bx: float, by: float,          # segment end (lon, lat)
    m_per_deg_lon: float | None = None,
) -> tuple[float, float, float, float]:
    """Planar point-to-segment distance with latitude scaling.

    Pass ``m_per_deg_lon`` precomputed from the query point to skip ~3 trig
    calls per segment — worth it when walking thousands of segments in a cell.

    Returns (distance_m, snapped_lon, snapped_lat, along_fraction).
    """
    if m_per_deg_lon is not None:
        mlon = m_per_deg_lon
    else:
        mlon = meters_per_deg_lon((py + ay + by) / 3.0)
    mlat = METERS_PER_DEG_LAT

    pxm, pym = px * mlon, py * mlat
    axm, aym = ax * mlon, ay * mlat
    bxm, bym = bx * mlon, by * mlat

    dx = bxm - axm
    dy = bym - aym
    len_sq = dx * dx + dy * dy

    if len_sq < 1e-12:
        t = 0.0
    else:
        dot = (pxm - axm) * dx + (pym - aym) * dy
        t = max(0.0, min(1.0, dot / len_sq))

    sxm = axm + t * dx
    sym = aym + t * dy
    distance = math.hypot(pxm - sxm, pym - sym)

    return distance, ax + t * (bx - ax), ay + t * (by - ay), t


def point_to_linestring_distance_meters(
    px: float, py: float,
    coords: tuple[tuple[float, float], ...],
) -> tuple[float, float, float, int, float]:
    """Minimum distance from a point to an open linestring.

    Returns (distance_m, snapped_lon, snapped_lat, segment_index, along_fraction).
    A single-vertex linestring degenerates to the point distance.
    """
    if not coords:
        return float("inf"), px, py, 0, 0.0
    if len(coords) == 1:
        d = haversine_meters(py, px, coords[0][1], coords[0][0])
        return d, coords[0][0], coords[0][1], 0, 0.0

    mlon = meters_per_deg_lon(py)
    min_dist = float("inf")
    best_lon, best_lat = coords[0]
    best_seg = 0
    best_t = 0.0

    for i in range(len(coords) - 1):
        dist, slon, slat, t = point_to_segment_distance_meters(
            px, py,
            coords[i][0], coords[i][1],
            coords[i + 1][0], coords[i + 1][1],
            m_per_deg_lon=mlon,
        )
        if dist < min_dist:
            min_dist = dist
            best_lon, best_lat = slon, slat
            best_seg = i
            best_t = t

    return min_dist, best_lon, best_lat, best_seg, best_t


def point_in_polygon(px: float, py: float,
                     poly: tuple[tuple[float, float], ...]) -> bool:
    """Ray-casting containment test. Works on open or closed rings."""
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def point_to_ring_distance_meters(px: float, py: float,
                                  poly: tuple[tuple[float, float], ...]) -> float:
    """Distance from a point to a polygon's *boundary*, ignoring containment."""
    if len(poly) < 2:
        if not poly:
            return float("inf")
        return haversine_meters(py, px, poly[0][1], poly[0][0])

    mlon = meters_per_deg_lon(py)
    min_dist = float("inf")
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        dist, _, _, _ = point_to_segment_distance_meters(
            px, py, ax, ay, bx, by, m_per_deg_lon=mlon,
        )
        if dist < min_dist:
            min_dist = dist
    return min_dist


def point_to_polygon_distance_meters(px: float, py: float,
                                     poly: tuple[tuple[float, float], ...]) -> float:
    """Distance from a point to a filled polygon. Zero when inside."""
    if point_in_polygon(px, py, poly):
        return 0.0
    return point_to_ring_distance_meters(px, py, poly)


def within_bbox_meters(px: float, py: float,
                       poly: tuple[tuple[float, float], ...],
                       meters: float) -> bool:
    """Could this polygon be within `meters` of the point?

    A cheap conservative reject: compares against the polygon's bounding box,
    never returning False for something that is actually in range. Scanning a
    dense H3 cell means thousands of footprints, almost all of them far away,
    and this culls them for the cost of a min/max instead of a full edge walk.
    """
    if not poly:
        return False
    dlat = meters / METERS_PER_DEG_LAT
    dlon = meters / meters_per_deg_lon(py)
    lo_x = px - dlon
    hi_x = px + dlon
    lo_y = py - dlat
    hi_y = py + dlat
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for x, y in poly:
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y
    return not (max_x < lo_x or min_x > hi_x or max_y < lo_y or min_y > hi_y)


def _orientation(ax: float, ay: float, bx: float, by: float,
                 cx: float, cy: float) -> int:
    """Sign of the cross product (b-a) x (c-a): 1 ccw, -1 cw, 0 collinear."""
    val = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if val > 1e-15:
        return 1
    if val < -1e-15:
        return -1
    return 0


def segments_intersect(a: tuple[float, float], b: tuple[float, float],
                       c: tuple[float, float], d: tuple[float, float]) -> bool:
    """Do segments a-b and c-d cross?

    Proper intersections only — segments that merely touch at a shared endpoint
    are not counted, so a sightline grazing a building corner isn't treated as
    blocked.
    """
    o1 = _orientation(a[0], a[1], b[0], b[1], c[0], c[1])
    o2 = _orientation(a[0], a[1], b[0], b[1], d[0], d[1])
    o3 = _orientation(c[0], c[1], d[0], d[1], a[0], a[1])
    o4 = _orientation(c[0], c[1], d[0], d[1], b[0], b[1])
    return o1 != o2 and o3 != o4 and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0


def segment_crosses_ring(a: tuple[float, float], b: tuple[float, float],
                         poly: tuple[tuple[float, float], ...]) -> bool:
    """Does segment a-b cross any edge of a polygon ring?"""
    n = len(poly)
    if n < 2:
        return False
    for i in range(n):
        if segments_intersect(a, b, poly[i], poly[(i + 1) % n]):
            return True
    return False
