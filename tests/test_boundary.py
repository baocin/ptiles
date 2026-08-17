"""Every region file carries its own boundary polygon.

The point is identification and de-duplication without the admin layer. A header
bbox separates Alaska from Delaware, but it cannot separate two overlapping
regional extracts: Kanto's box and Chubu's box both contain Tokyo, so a bbox
alone cannot say which file owns a point there. The polygon can.

Absent is a first-class answer: every file published before the field reads 0/0
and must keep working off its bbox.
"""

import struct
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from ptiles.codec import decode_boundary  # noqa: E402

DATA = Path("/mnt/core/kino/ptiles/data")
SHIKOKU_PLACES = DATA / "states/JP-SHIKOKU.places_v1.ptiles"
KANTO_BUILDINGS = DATA / "v4/states/JP-KANTO.buildings_v9.ptiles"


# --- encoding --------------------------------------------------------------


def test_ptbd_roundtrip():
    from encoding import decode_boundary as dec, encode_boundary

    ring = [(139.0 + i * 0.01, 35.0 + i * 0.005) for i in range(40)]
    ring.append(ring[0])
    blob = encode_boundary([ring, ring[:12]])
    assert blob[:4] == b"PTBD"
    back = dec(blob)
    assert [len(r) for r in back] == [41, 12]
    assert abs(back[0][0][0] - 139.0) < 1e-5
    # Reader-side copy must agree with the writer's.
    assert [len(r) for r in decode_boundary(blob)] == [41, 12]


def test_ptbd_rejects_what_it_does_not_recognise():
    """Self-describing on purpose: the aux section is not, and a reader that
    guesses wrong there reads garbage."""
    from encoding import encode_boundary

    assert decode_boundary(b"") == []
    assert decode_boundary(b"XXXX\x01\x00\x00") == []
    bad_version = bytearray(encode_boundary([[(0, 0), (1, 1), (2, 0)]]))
    bad_version[4] = 99
    assert decode_boundary(bytes(bad_version)) == []


def test_degenerate_rings_are_dropped_not_encoded():
    from encoding import encode_boundary

    assert encode_boundary([]) == b""
    assert encode_boundary([[(0.0, 0.0), (1.0, 1.0)]]) == b""  # 2 vertices


# --- sources ---------------------------------------------------------------


def test_poly_parser_keeps_holes():
    """A dropped island reads as "not covered", which is the question this
    polygon exists to answer -- so holes are kept rather than discarded."""
    from boundaries import parse_poly

    text = (
        "japan\nring\n 1.0 2.0\n 1.1 2.1\n 1.2 2.0\nEND\n"
        "!hole\n 1.05 2.02\n 1.08 2.05\n 1.1 2.02\nEND\nEND\n"
    )
    assert len(parse_poly(text)) == 2


def test_cap_coarsens_rather_than_discarding():
    """Alaska is a mainland plus thousands of islands. Keeping the big rings and
    dropping the tail would silently lose coverage, so coarsen instead.

    The ring has to be genuinely wiggly: a collinear one simplifies to two
    vertices, below the 3-vertex floor, so coarsening cannot shrink it and the
    drop path is the only option left.
    """
    import math

    from boundaries import _cap

    def wiggly(n, r=1.0, phase=0.0):
        return [
            (
                r * (1 + 0.15 * math.sin(9 * (i / n) * math.tau + phase))
                * math.cos((i / n) * math.tau),
                r * (1 + 0.15 * math.sin(9 * (i / n) * math.tau + phase))
                * math.sin((i / n) * math.tau),
            )
            for i in range(n)
        ]

    rings = [wiggly(400, phase=k) for k in range(30)]
    capped = _cap(rings, budget=2000)
    assert len(capped) == len(rings), "no ring should have been dropped"
    assert sum(len(r) for r in capped) <= 2000


@pytest.mark.skipif(
    not Path(
        "/mnt/core/timeline-ptiles-cache/admin_data/states/cb_2023_us_state_500k.shp"
    ).exists(),
    reason="Census shapefile not cached",
)
def test_alaska_keeps_every_island():
    """The case that motivated coarsening: ~549 rings, none of them expendable."""
    from boundaries import us_state_boundary
    from encoding import BOUNDARY_MAX_VERTICES

    try:
        rings = us_state_boundary("AK")
    except Exception:
        pytest.skip("geopandas unavailable")
    if not rings:
        pytest.skip("no AK geometry")
    assert len(rings) > 100, f"expected Alaska's islands, got {len(rings)} rings"
    assert sum(len(r) for r in rings) <= BOUNDARY_MAX_VERTICES


@pytest.mark.skipif(
    not Path("/mnt/core/timeline-ptiles-cache/boundaries/shikoku.poly").exists()
    and not Path("/mnt/core/timeline-ptiles-cache/admin_data/states").exists(),
    reason="no boundary sources cached",
)
def test_geofabrik_boundary_is_the_extract_cut():
    from boundaries import geofabrik_boundary

    rings = geofabrik_boundary("shikoku")
    if not rings:
        pytest.skip("shikoku.poly not cached and no network")
    lats = [p[1] for r in rings for p in r]
    lons = [p[0] for r in rings for p in r]
    assert 32 < min(lats) and max(lats) < 35, (min(lats), max(lats))
    assert 131 < min(lons) and max(lons) < 136, (min(lons), max(lons))


# --- in the files ----------------------------------------------------------


@pytest.mark.skipif(not SHIKOKU_PLACES.exists(), reason="Shikoku not built")
def test_a_built_file_carries_its_boundary():
    from ptiles.places import PlacesReader

    r = PlacesReader.open(SHIKOKU_PLACES)
    h = r.header
    assert h["boundary_offset"] > 0 and h["boundary_length"] > 0
    rings = r.boundary
    assert rings
    # The polygon must sit inside the bbox the same header declares.
    for ring in rings:
        for lon, lat in ring:
            assert h["min_lat"] - 0.01 <= lat <= h["max_lat"] + 0.01
            assert h["min_lon"] - 0.01 <= lon <= h["max_lon"] + 0.01


@pytest.mark.skipif(not KANTO_BUILDINGS.exists(), reason="no Kanto build")
def test_a_file_written_before_the_field_still_opens():
    """Zeros mean absent, and the reader falls back to the bbox."""
    from ptiles.buildings import BuildingsReader

    r = BuildingsReader.open(KANTO_BUILDINGS)
    if r.header["boundary_length"]:
        pytest.skip("this file has been rebuilt since")
    assert r.boundary == []
    assert r.header["max_lat"] > r.header["min_lat"]  # bbox still usable


@pytest.mark.skipif(not SHIKOKU_PLACES.exists(), reason="Shikoku not built")
def test_appending_the_boundary_did_not_disturb_the_data():
    """The block goes on the end of a finished file, so nothing else moves."""
    from ptiles.places import PlacesReader

    r = PlacesReader.open(SHIKOKU_PLACES)
    total = sum(len(r.get_in_cell(e["h3_cell"])) for e in r._index)
    assert total == r.header["feature_count"]


def test_all_layers_expose_boundary():
    """Readers that are not BlockFileReader pick it up from BoundaryMixin."""
    from ptiles.admin import AdminReader
    from ptiles.buildings import BuildingsReader
    from ptiles.business import BusinessReader
    from ptiles.ev import EvReader
    from ptiles.parks import ParkReader
    from ptiles.places import PlacesReader
    from ptiles.points import PointLayerReader
    from ptiles.rail import RailReader
    from ptiles.roads import PtlrRoadsReader
    from ptiles.trails import TrailsReader
    from ptiles.water import WaterReader

    for cls in (AdminReader, BuildingsReader, BusinessReader, EvReader, ParkReader,
                PlacesReader, PointLayerReader, RailReader, TrailsReader,
                WaterReader, PtlrRoadsReader):
        assert hasattr(cls, "boundary"), cls.__name__


# --- the actual point: telling overlapping extracts apart -------------------


@pytest.mark.skipif(not SHIKOKU_PLACES.exists(), reason="Shikoku not built")
def test_polygon_narrows_where_the_bbox_cannot():
    """A point inside the bbox but outside the polygon is not covered.

    Shikoku's bbox spans 131.76..135.18E / 32.22..34.66N, a rectangle that
    includes sea and slices of neighbouring regions. The polygon excludes them.
    """
    from ptiles.composite import PlaceLayer

    layer = PlaceLayer(SHIKOKU_PLACES, "JP-SHIKOKU")
    assert layer.region_boundary, "expected a boundary on this file"

    h = layer._reader.header
    # A corner of the bbox: inside the rectangle, well outside the island.
    corner_lat = h["min_lat"] + 0.02
    corner_lon = h["min_lon"] + 0.02
    s, w, n, e = layer.bounds
    assert s <= corner_lat <= n and w <= corner_lon <= e, "corner is inside the bbox"
    assert not layer.covers(corner_lat, corner_lon), (
        "the polygon should exclude a bbox corner that is not on the island"
    )

    # Matsuyama, on Shikoku, is covered.
    assert layer.covers(33.8392, 132.7657)


@pytest.mark.skipif(not SHIKOKU_PLACES.exists(), reason="Shikoku not built")
def test_padded_queries_keep_the_bbox_semantics():
    """A padded query asks about the neighbourhood, so it must not be narrowed
    by the polygon -- a nearest-feature search legitimately reaches offshore."""
    from ptiles.composite import PlaceLayer

    layer = PlaceLayer(SHIKOKU_PLACES, "JP-SHIKOKU")
    h = layer._reader.header
    corner_lat, corner_lon = h["min_lat"] + 0.02, h["min_lon"] + 0.02
    assert layer.covers(corner_lat, corner_lon, pad=0.05)
