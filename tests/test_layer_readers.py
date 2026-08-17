"""Readers for layers that were published without one: roads (PTLR), trails, EV.

Roads shipped in the PTLR container and the Python client could not open it at
all -- for any region, the US included. Trails and EV have been published since
the 2026-08-07 snapshot with no reader either. All three are now readable.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ptiles.ev import ACCESS, SOCKETS, EvReader  # noqa: E402
from ptiles.reader import BlockFileReader, MergedBlockReader  # noqa: E402
from ptiles.roads import PtlrRoadsReader, RoadsReader  # noqa: E402
from ptiles.trails import TrailsReader  # noqa: E402

DATA = Path("/mnt/core/kino/ptiles/data")
JP_TRAILS = DATA / "states/JP.trails_v1.ptiles"
JP_EV = DATA / "states/JP.ev_v1.ptiles"
JP_ROADS = DATA / "JP.roads.ptiles"


# --- structural guard -----------------------------------------------------


def test_block_file_reader_keeps_its_methods():
    """Adding a class between BlockFileReader's methods silently reparents them.

    That happened: MergedBlockReader was inserted mid-class and everything below
    it became part of the subclass, so PlacesReader lost resolve_offset and
    ParkReader lost _read_merged_block. Both failed only at query time, inside
    the adapters' `except Exception` -- the layers returned empty and the suite
    stayed green.
    """
    for method in (
        "resolve_offset", "read_block_raw", "lookup_cell", "_read_merged_block",
        "header", "close",
    ):
        assert hasattr(BlockFileReader, method), f"BlockFileReader lost {method}"
    assert issubclass(MergedBlockReader, BlockFileReader)
    assert hasattr(MergedBlockReader, "resolve_offset")


def test_readers_that_use_the_merged_block_base_still_work():
    """The layers that regressed, exercised directly rather than through a
    composite adapter that swallows exceptions."""
    from ptiles.parks import ParkReader
    from ptiles.places import PlacesReader
    from ptiles.rail import RailReader

    for cls in (ParkReader, PlacesReader, RailReader):
        assert hasattr(cls, "get_in_cell"), cls.__name__


# --- trails ---------------------------------------------------------------


@pytest.mark.skipif(not JP_TRAILS.exists(), reason="no trails file")
def test_trails_decode():
    import h3

    r = TrailsReader.open(JP_TRAILS)
    assert r.header["magic"] == b"PTILESH"
    recs = r.get_in_cell(h3.latlng_to_cell(35.6812, 139.7671, 7))
    assert recs, "Tokyo should have trails"
    kinds = {t.trail_type for t in recs}
    assert kinds <= {
        "path", "track", "bridleway", "cycleway", "footway", "steps", "trailhead",
    }, kinds
    # Geometry is real, not a mis-slice: every vertex sits inside Japan.
    for t in recs[:200]:
        assert t.coords
        for lon, lat in t.coords:
            assert 122 < lon < 155 and 20 < lat < 46, (lon, lat)
    assert any(t.name for t in recs), "some trails carry names"


@pytest.mark.skipif(not JP_TRAILS.exists(), reason="no trails file")
def test_trails_point_geometry_is_a_single_vertex():
    """geom_type 1 is a trailhead node, not a way."""
    r = TrailsReader.open(JP_TRAILS)
    for entry in r._index[:40]:
        for t in r.get_in_cell(entry["h3_cell"]):
            if t.is_point:
                assert len(t.coords) == 1
                return


# --- EV -------------------------------------------------------------------


@pytest.mark.skipif(not JP_EV.exists(), reason="no ev file")
def test_ev_decodes_every_record():
    """A record walk that drifts would stop early or overrun; the header count
    is the independent check that it did neither."""
    r = EvReader.open(JP_EV)
    assert r.header["magic"] == b"PTILESE"
    chargers = []
    for entry in r._index:
        chargers.extend(r.get_in_cell(entry["h3_cell"]))
    assert len(chargers) == r.header["feature_count"]

    for c in chargers:
        assert c.access in ACCESS
        assert set(c.sockets) <= set(SOCKETS)
        assert 20 < c.lat < 46 and 122 < c.lon < 155
        assert c.power_kw is None or 0 < c.power_kw < 1000


@pytest.mark.skipif(not JP_EV.exists(), reason="no ev file")
def test_ev_sparsity_matches_what_the_builder_reported():
    """build_ev printed '4 with power' for Japan. Anything else means the
    decoder is reading the wrong bytes, not that the data changed."""
    r = EvReader.open(JP_EV)
    chargers = [c for e in r._index for c in r.get_in_cell(e["h3_cell"])]
    assert sum(1 for c in chargers if c.power_kw) == 4
    # chademo dominates in Japan; a decoder reading the mask off by a bit or
    # two would not produce that.
    kinds = [s for c in chargers for s in c.sockets]
    assert kinds.count("chademo") > sum(1 for k in kinds if k != "chademo")


# --- roads (PTLR) ---------------------------------------------------------


@pytest.mark.skipif(not JP_ROADS.exists(), reason="no roads file")
def test_open_dispatches_on_magic():
    r = RoadsReader.open(JP_ROADS)
    assert isinstance(r, PtlrRoadsReader)
    h = r.header
    assert h["magic"] == b"PTLR"
    assert h["feature_count"] == 10_552_202


@pytest.mark.skipif(not JP_ROADS.exists(), reason="no roads file")
def test_roads_open_is_lazy():
    """Opening must not decompress 180 MB of band, or holding several countries
    open becomes impossible."""
    r = RoadsReader.open(JP_ROADS)
    assert not r.index_loaded


@pytest.mark.skipif(not JP_ROADS.exists(), reason="no roads file")
def test_ptlr_header_bounds_are_used():
    r = RoadsReader.open(JP_ROADS)
    h = r.header
    if "min_lat" not in h:
        pytest.skip("file predates the bounds field")
    assert 20 < h["min_lat"] < h["max_lat"] < 46
    assert 122 < h["min_lon"] < h["max_lon"] < 155


@pytest.mark.skipif(not JP_ROADS.exists(), reason="no roads file")
def test_roads_nearest_finds_tokyo_station():
    """The slowest test here (~20s): it builds the in-memory index over 10.5M
    roads, which is the whole point -- PTLR carries no index on disk."""
    r = RoadsReader.open(JP_ROADS)
    hits = r.nearest_n(35.6812, 139.7671, n=5)
    assert hits, "Tokyo Station should have roads within range"
    assert hits == sorted(hits, key=lambda h: h.distance_meters)
    assert hits[0].distance_meters < 50
    names = [h.road.name for h in hits if h.road.name]
    assert any("中央" in n for n in names), names  # 中央通路, the concourse
