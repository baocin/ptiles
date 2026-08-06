"""
Nearest-feature search across any per-cell PTiles layer.

The layers are keyed by H3 res-7 cell, so "what is the nearest X" is a spiral:
read the cell you are standing in, then ring 1, then ring 2, stopping as soon
as the answer cannot improve. For a common chain that is one block (~1-5 KB);
the alternative — reading a whole state's worth of candidates and sorting —
costs megabytes.

Stopping early is the part that needs care. Finding `n` matches in ring `k` is
not a reason to stop: a cell in ring `k+1` can still hold something closer,
because a match can sit at the far corner of a near cell. So the loop keeps
going until the worst kept match is closer than *any* unscanned cell can be,
measured against the real H3 cell boundaries rather than an assumed radius.

    from ptiles.nearest import nearest
    hits = nearest(business_reader, 30.2672, -97.7431,
                   predicate=match_name("Taco Bell"), n=3)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol

import h3

from ptiles.geometry import (
    haversine_meters,
    point_to_linestring_distance_meters,
    point_to_polygon_distance_meters,
    point_to_ring_distance_meters,
)

logger = logging.getLogger("ptiles.nearest")

H3_RES = 7

# Hard ceiling on the spiral. Ring 12 at res 7 is roughly a 25 km radius; past
# that a per-cell scan is the wrong tool and the caller should say so rather
# than quietly walking a whole state.
DEFAULT_MAX_RINGS = 12


class CellReader(Protocol):
    """Any layer reader that can hand back the features in one H3 cell."""

    def get_in_cell(self, cell: int | str) -> list[Any]: ...


@dataclass(frozen=True, slots=True)
class NearestHit:
    feature: Any
    distance_meters: float
    cell: int


@dataclass(slots=True)
class NearestResult:
    """Hits plus what it cost to find them.

    Iterating or indexing the result gives the hits, so callers that only want
    the answer can ignore the counters.
    """

    hits: list[NearestHit] = field(default_factory=list)
    rings_scanned: int = 0
    cells_read: int = 0
    features_examined: int = 0
    # True when the spiral hit max_rings or max_meters with fewer than n hits,
    # i.e. "no more were found", not "no more exist".
    exhausted: bool = False

    def __iter__(self) -> Iterator[NearestHit]:
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def __getitem__(self, i):
        return self.hits[i]

    def __bool__(self) -> bool:
        return bool(self.hits)


# --- Distance dispatch -------------------------------------------------------


def feature_distance_meters(lat: float, lon: float, feature: Any) -> float:
    """Distance from a point to a decoded feature, whatever its geometry.

    Point layers (business, camera, signals, places) carry scalar lat/lon.
    Line layers (roads, rail, rivers) and area layers (buildings, parks, lakes)
    carry a coordinate tuple, and must be measured to the geometry — a lake's
    centroid can be kilometres from its shore.
    """
    # Point features.
    f_lat = getattr(feature, "lat", None)
    f_lon = getattr(feature, "lon", None)
    if f_lat is not None and f_lon is not None:
        return haversine_meters(lat, lon, f_lat, f_lon)

    coords = getattr(feature, "coordinates", None)
    if coords is None:
        coords = getattr(feature, "coords", None)
    if not coords:
        # Last resort: a centroid, if the decoder computed one.
        c_lat = getattr(feature, "centroid_lat", None)
        c_lon = getattr(feature, "centroid_lon", None)
        if c_lat is not None and c_lon is not None:
            return haversine_meters(lat, lon, c_lat, c_lon)
        return float("inf")

    if _is_closed_ring(coords):
        # Buildings arrive open, parks/lakes closed; both are areas.
        return point_to_polygon_distance_meters(lon, lat, coords)

    if getattr(feature, "coordinates", None) is not None:
        # `coordinates` is the buildings field and is always a footprint,
        # even when the ring isn't explicitly closed.
        return point_to_polygon_distance_meters(lon, lat, coords)

    dist, _, _, _, _ = point_to_linestring_distance_meters(lon, lat, coords)
    return dist


def _is_closed_ring(coords: tuple[tuple[float, float], ...]) -> bool:
    return len(coords) > 3 and coords[0] == coords[-1]


# --- Predicate helpers -------------------------------------------------------


def match_name(text: str, *, exact: bool = False,
               fields: tuple[str, ...] = ("name", "brand")) -> Callable[[Any], bool]:
    """Case-insensitive match on a feature's name or brand.

    Prefix-matching rather than substring by default, so "Taco Bell" also
    catches "Taco Bell Cantina" without also catching "Not A Taco Bell".
    """
    needle = text.casefold()

    def predicate(feature: Any) -> bool:
        for attr in fields:
            value = getattr(feature, attr, None)
            if not value:
                continue
            folded = value.casefold()
            if folded == needle or (not exact and folded.startswith(needle)):
                return True
        return False

    return predicate


def match_attr(attr: str, *values: Any) -> Callable[[Any], bool]:
    """Match a feature whose attribute equals any of the given values.

    e.g. match_attr("water_type", "river", "stream")
    """
    wanted = set(values)

    def predicate(feature: Any) -> bool:
        return getattr(feature, attr, None) in wanted

    return predicate


# --- The spiral --------------------------------------------------------------


def _ring_lower_bound_meters(lat: float, lon: float, ring_cells: list[str]) -> float:
    """Closest any feature in these cells could possibly be.

    Uses the cells' true H3 boundaries, so it stays honest across the res-7
    size variation and near the icosahedron distortion, where a nominal
    "1.2 km per ring" would not.
    """
    best = float("inf")
    for cell in ring_cells:
        boundary = tuple((lng, la) for la, lng in h3.cell_to_boundary(cell))
        d = point_to_ring_distance_meters(lon, lat, boundary)
        if d < best:
            best = d
    return best


def nearest(
    reader: CellReader,
    lat: float,
    lon: float,
    *,
    predicate: Callable[[Any], bool] | None = None,
    n: int = 1,
    max_rings: int = DEFAULT_MAX_RINGS,
    max_meters: float | None = None,
    distance_fn: Callable[[float, float, Any], float] = feature_distance_meters,
) -> NearestResult:
    """Find the `n` features nearest to (lat, lon) that satisfy `predicate`.

    Reads one H3 ring at a time and stops as soon as the answer is provably
    settled — see the module docstring for why "found n" alone is not enough.

    Args:
        reader: any layer reader exposing ``get_in_cell``.
        predicate: filter on the decoded feature; None accepts everything.
        n: how many hits to return, nearest first.
        max_rings: give up after this many rings (~2 km per ring at res 7).
        max_meters: give up once the spiral is provably beyond this radius.
        distance_fn: override the geometry dispatch.

    Returns:
        NearestResult — iterable of NearestHit, plus counters describing the
        work done. ``exhausted`` is True when the search ran out of room
        before collecting `n`.
    """
    if n < 1:
        raise ValueError("n must be >= 1")

    center = h3.latlng_to_cell(lat, lon, H3_RES)
    result = NearestResult()
    hits: list[NearestHit] = []
    seen: set[str] = set()

    for k in range(max_rings + 1):
        ring = [c for c in h3.grid_ring(center, k) if c not in seen]
        seen.update(ring)
        result.rings_scanned = k

        for cell in ring:
            cell_int = int(cell, 16)
            try:
                features = reader.get_in_cell(cell_int)
            except Exception as e:
                logger.warning("nearest: cell %s failed to read: %s", cell, e)
                continue
            result.cells_read += 1
            if not features:
                continue
            for feature in features:
                result.features_examined += 1
                if predicate is not None and not predicate(feature):
                    continue
                d = distance_fn(lat, lon, feature)
                if max_meters is not None and d > max_meters:
                    continue
                hits.append(NearestHit(feature, d, cell_int))

        hits.sort(key=lambda h: h.distance_meters)
        del hits[n:]

        # How close could anything we haven't looked at yet be? Everything
        # unscanned lies in ring k+1 or beyond, so the nearest ring-(k+1) cell
        # bounds it.
        next_ring = [c for c in h3.grid_ring(center, k + 1) if c not in seen]
        if not next_ring:
            result.exhausted = True
            break
        bound = _ring_lower_bound_meters(lat, lon, next_ring)

        if max_meters is not None and bound > max_meters:
            # Nothing further out can be within the caller's radius.
            break

        if len(hits) >= n and hits[-1].distance_meters <= bound:
            break
    else:
        # Fell out of the loop without settling — we stopped on max_rings.
        result.exhausted = True

    if len(hits) < n:
        result.exhausted = True

    result.hits = hits
    return result
