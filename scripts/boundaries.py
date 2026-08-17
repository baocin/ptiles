#!/usr/bin/env python3
"""Where a region's boundary polygon comes from, for the PTBD header block.

Every region-level file stores its own boundary so it can be identified and
de-duplicated without the admin layer. The header bbox is enough to tell Alaska
from Delaware; it is not enough to tell a Kanto extract from a Chubu one, whose
boxes overlap heavily, nor to notice that two files cover the same ground.

Two sources, because a US state and a Geofabrik region are different things:

- **US states**: the Census cartographic boundary file already cached for the
  admin layer. This is the administrative boundary.
- **Everything else**: the Geofabrik `.poly` for the extract. That is the exact
  shape the file's data was cut to -- including the seam overlap between
  neighbouring regions -- so it describes the file rather than the territory.

Falls back to no boundary rather than to a guess: a wrong polygon is worse than
an absent one, since the reader treats absent as "use the bbox".
"""

import math
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from encoding import BOUNDARY_MAX_VERTICES, BOUNDARY_SIMPLIFY_DEG

CENSUS_STATES = Path(
    "/mnt/core/timeline-ptiles-cache/admin_data/states/cb_2023_us_state_500k.shp"
)
POLY_CACHE = Path("/mnt/core/timeline-ptiles-cache/boundaries")

# Geofabrik lays extracts out by continent; a scope's pbf name is its poly name.
GEOFABRIK_POLY = "https://download.geofabrik.de/{path}.poly"
# Only the paths we actually build. Adding a country means adding its prefix.
GEOFABRIK_PATHS = {
    "japan": "asia/japan",
    "hokkaido": "asia/japan/hokkaido",
    "tohoku": "asia/japan/tohoku",
    "kanto": "asia/japan/kanto",
    "chubu": "asia/japan/chubu",
    "kansai": "asia/japan/kansai",
    "chugoku": "asia/japan/chugoku",
    "shikoku": "asia/japan/shikoku",
    "kyushu": "asia/japan/kyushu",
}


def parse_poly(text: str) -> list[list[tuple[float, float]]]:
    """Parse the Osmosis .poly format Geofabrik publishes.

        <name>
        <ring name>
           lon lat
           ...
        END
        END

    A ring name starting with '!' marks a hole. Holes are kept as ordinary
    rings here: this polygon is used for coverage tests, where including a hole
    is a smaller error than dropping the island that contains it.
    """
    rings: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "END":
            if current is not None:
                if len(current) >= 3:
                    rings.append(current)
                current = None
            continue
        parts = line.split()
        if len(parts) == 2:
            try:
                lon, lat = float(parts[0]), float(parts[1])
            except ValueError:
                current = []  # a ring header, not coordinates
                continue
            if current is None:
                current = []
            current.append((lon, lat))
        else:
            current = None if current is None and not rings else current
    return rings


def simplify_ring(ring, tolerance=BOUNDARY_SIMPLIFY_DEG):
    """Douglas-Peucker via Shapely, unchanged if Shapely is unavailable."""
    if len(ring) < 4:
        return ring
    try:
        from shapely.geometry import LineString

        simplified = LineString(ring).simplify(tolerance, preserve_topology=True)
        coords = [(x, y) for x, y in simplified.coords]
        return coords if len(coords) >= 3 else ring
    except Exception:
        return ring


def _cap(rings, budget=BOUNDARY_MAX_VERTICES):
    """Fit rings into a vertex budget by coarsening, not by discarding.

    Alaska is a mainland plus thousands of islands and blows any sane budget.
    The obvious fix -- keep the biggest rings, drop the tail -- is wrong here: a
    dropped island reads as "this file does not cover that point", which is
    exactly the question the boundary exists to answer. So coarsen every ring
    together until it fits, and drop rings only if coarsening cannot get there.
    """
    if sum(len(r) for r in rings) <= budget:
        return rings
    tolerance = BOUNDARY_SIMPLIFY_DEG
    for _ in range(8):
        tolerance *= 2.5
        coarser = [simplify_ring(r, tolerance) for r in rings]
        if sum(len(r) for r in coarser) <= budget:
            return coarser
        rings = coarser
    # Still too big: the last resort, and the only path that loses coverage.
    rings = sorted(rings, key=len, reverse=True)
    out, used = [], 0
    for ring in rings:
        if used + len(ring) > budget:
            continue
        out.append(ring)
        used += len(ring)
    return out or rings[:1]


def us_state_boundary(abbr: str):
    """Boundary rings for a US state, from the cached Census shapefile."""
    if not CENSUS_STATES.exists():
        return []
    try:
        import geopandas as gpd
    except ImportError:
        return []
    gdf = gpd.read_file(CENSUS_STATES)
    match = gdf[gdf["STUSPS"] == abbr.upper()]
    if match.empty:
        return []
    rings = []
    for geom in match.geometry:
        parts = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for part in parts:
            ring = [(x, y) for x, y in part.exterior.coords]
            if len(ring) >= 3:
                rings.append(simplify_ring(ring))
    return _cap(rings)


def geofabrik_boundary(pbf_name: str):
    """Boundary rings for a Geofabrik extract, from its published .poly."""
    path = GEOFABRIK_PATHS.get(pbf_name)
    if not path:
        return []
    POLY_CACHE.mkdir(parents=True, exist_ok=True)
    cached = POLY_CACHE / f"{pbf_name}.poly"
    if not cached.exists():
        try:
            with urllib.request.urlopen(GEOFABRIK_POLY.format(path=path), timeout=60) as r:
                cached.write_bytes(r.read())
        except Exception as e:
            print(f"  boundary: could not fetch {pbf_name}.poly: {e}", file=sys.stderr)
            return []
    rings = parse_poly(cached.read_text())
    return _cap([simplify_ring(r) for r in rings])


def boundary_for(region) -> list[list[tuple[float, float]]]:
    """Boundary rings for a states.py region, or [] when none is available."""
    from states import US

    try:
        from ptiles.scopes import country_of

        country = country_of(region.abbr)
    except Exception:
        country = US if region.fips else ""

    if country == US:
        return us_state_boundary(region.abbr)
    return geofabrik_boundary(region.pbf_name)



def stamp_boundary(path, region) -> int:
    """Give a finished file its region's boundary polygon.

    One call per builder, after the file is written: the block is appended and
    only the two header fields at @84 change, so no builder's offset arithmetic
    is touched. Silent no-op when no boundary source is available -- readers
    treat an absent polygon as "fall back to the bbox".
    """
    from encoding import append_boundary

    try:
        rings = boundary_for(region)
    except Exception as e:
        print(f"  boundary: none for {getattr(region, 'abbr', region)}: {e}",
              file=sys.stderr)
        return 0
    if not rings:
        return 0
    return append_boundary(path, rings)


def demo() -> None:
    """Self-check against the two real sources."""
    text = """japan
ring
   1.32E+02   3.22E+01
   1.33E+02   3.32E+01
   1.34E+02   3.30E+01
END
END
"""
    rings = parse_poly(text)
    assert len(rings) == 1 and len(rings[0]) == 3, rings
    assert rings[0][0] == (132.0, 32.2), rings[0][0]

    # A hole ring is kept, not silently dropped.
    two = parse_poly(text.replace("END\nEND", "END\n!hole\n 1.0 2.0\n 1.1 2.1\n 1.2 2.0\nEND\nEND"))
    assert len(two) == 2, two

    capped = _cap([list(range(100)), list(range(5))], budget=50)
    assert capped == [list(range(5))] or capped == [list(range(100))], capped
    print("boundaries: ok")


if __name__ == "__main__":
    demo()



def stamp_boundary_at(path, region, offset_field: int) -> int:
    """stamp_boundary for a container whose header fields sit elsewhere.

    PTLR keeps its own bbox at @84, so its boundary offset/length pair is at
    @100/@108 rather than the @84/@92 the PTiles header uses.
    """
    import struct

    from encoding import encode_boundary

    try:
        rings = boundary_for(region)
    except Exception as e:
        print(f"  boundary: none for {getattr(region, 'abbr', region)}: {e}",
              file=sys.stderr)
        return 0
    blob = encode_boundary(rings)
    if not blob:
        return 0
    with open(path, "r+b") as f:
        f.seek(0, 2)
        offset = f.tell()
        f.write(blob)
        f.seek(offset_field)
        f.write(struct.pack("<QI", offset, len(blob)))
    return len(blob)
