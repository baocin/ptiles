"""Guards for the things that break as more countries are added.

Each test here corresponds to a defect found by asking "what happens at country
three, or thirty" rather than to a reported bug:

* a country whose ISO code is already a US state abbreviation
* two countries sitting at different layer versions
* opening a country you never query
* a country that crosses the antimeridian
* roads files with no recorded bounds
"""

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from ptiles.composite import lon_ranges_overlap, lon_within  # noqa: E402
from ptiles.scopes import (  # noqa: E402
    check_country_scope,
    collides_with_us_state,
    country_of,
    publish_relpath,
)

SNAPSHOT = Path("/mnt/core/kino/ptiles/data")
JP_KANTO = SNAPSHOT / "v4/states/JP-KANTO.buildings_v9.ptiles"
AK_BUILDINGS = SNAPSHOT / "v4/states/AK.buildings_v9.ptiles"


# --- collisions between country codes and US states -----------------------


def test_the_26_real_collisions_are_all_detected():
    """The first hand-written list of these named 10 of 26 and was shipped."""
    real = {
        "AL", "AR", "AZ", "CA", "CO", "DE", "GA", "ID", "IL", "IN", "KY", "LA",
        "MA", "MD", "ME", "MN", "MO", "MS", "MT", "NC", "NE", "PA", "SC", "SD",
        "TN", "VA",
    }
    for code in real:
        assert collides_with_us_state(code), code
    for code in ("JP", "FR", "BR", "ZA", "CN"):
        assert not collides_with_us_state(code), code


def test_alpha3_is_never_ambiguous():
    assert country_of("CAN") == "CAN"
    assert country_of("DEU") == "DEU"
    assert country_of("CAN-ON") == "CAN"
    assert publish_relpath("CAN", "CAN.places_v1.ptiles") == "CAN/CAN.places_v1.ptiles"


def test_canada_no_longer_lands_on_california():
    """`CA.places_v1.ptiles` for Canada is byte-identical in name and path to
    California's, so publishing it would overwrite California in the bucket."""
    california = publish_relpath("CA", "CA.buildings_v9.ptiles")
    canada = publish_relpath("CAN", "CAN.buildings_v9.ptiles")
    assert california != canada
    assert "/" not in california  # US stays at the snapshot root
    assert canada.startswith("CAN/")


@pytest.mark.parametrize("bad", ["CA", "TN", "DE", "IN"])
def test_declaring_a_colliding_country_is_refused(bad):
    with pytest.raises(ValueError, match="US state"):
        check_country_scope(bad)


@pytest.mark.parametrize("good", ["JP", "JP-KANTO", "CAN", "CA-ON", "FR"])
def test_unambiguous_country_scopes_are_accepted(good):
    check_country_scope(good)


def test_the_region_table_guards_itself():
    """states.py refuses a colliding row at import, with no ptiles import."""
    import states

    bad = states.State("", "CA", "Canada", -141.0, 41.6, -52.6, 83.2, "canada")
    states.NON_US.append(bad)
    try:
        with pytest.raises(ValueError, match="collides with the US state"):
            states._check_non_us_scopes()
    finally:
        states.NON_US.remove(bad)


# --- which scopes a country expands to -------------------------------------


def test_country_expands_to_a_covering_set_not_every_scope():
    """Japan declares JP *and* 8 regions because different layers need each.

    Expanding `--countries JP` to all nine would build a country-wide rail file
    and eight regional ones covering the same track, so a client opening Japan
    counts every feature twice. (This is not hypothetical: the first version of
    the flag did exactly that and wrote four duplicate rail files.)
    """
    from states import scopes_for_country

    assert scopes_for_country("JP") == ["JP"]
    regions = scopes_for_country("JP", subdivisions=True)
    assert regions == [
        "JP-CHUBU", "JP-CHUGOKU", "JP-HOKKAIDO", "JP-KANSAI",
        "JP-KANTO", "JP-KYUSHU", "JP-SHIKOKU", "JP-TOHOKU",
    ]
    assert "JP" not in regions


def test_us_expands_to_its_states():
    """The US declares no country-wide scope, so the 51 states are the set."""
    from states import scopes_for_country

    us = scopes_for_country("US")
    assert len(us) == 51
    assert "TN" in us and "DC" in us


def test_unknown_country_expands_to_nothing():
    from states import scopes_for_country

    assert scopes_for_country("FR") == []


# --- layer versions differing between countries ---------------------------


def run_manifest(build_dir):
    return subprocess.run(
        [sys.executable, str(REPO / "scripts/gen_manifest.py"), str(build_dir),
         "2026-08-20", "test"],
        capture_output=True, text=True,
    )


@pytest.mark.skipif(not AK_BUILDINGS.exists(), reason="no build data")
def test_two_countries_at_different_versions_publish_together(tmp_path):
    """Countries are rebuilt on their own schedules; one reaching v10 while
    another sits at v9 used to abort the whole publish."""
    (tmp_path / "AK.buildings_v9.ptiles").symlink_to(AK_BUILDINGS)
    (tmp_path / "FR.buildings_v10.ptiles").symlink_to(AK_BUILDINGS)
    r = run_manifest(tmp_path)
    assert r.returncode == 0, r.stderr
    entry = json.loads(r.stdout)["layers"]["buildings"]
    assert entry["versions"] == {"FR": 10, "US": 9}
    # Ambiguous at the layer level, so a client must use each scope's own path.
    assert entry["version"] is None and entry["pattern"] is None
    assert entry["scopes"]["FR"]["path"] == "FR/FR.buildings_v10.ptiles"
    assert entry["scopes"]["AK"]["version"] == 9


@pytest.mark.skipif(not AK_BUILDINGS.exists(), reason="no build data")
def test_one_country_at_two_versions_is_still_fatal(tmp_path):
    """That really is broken -- the filename would be unpredictable."""
    (tmp_path / "AK.buildings_v9.ptiles").symlink_to(AK_BUILDINGS)
    (tmp_path / "TX.buildings_v10.ptiles").symlink_to(AK_BUILDINGS)
    r = run_manifest(tmp_path)
    assert r.returncode != 0
    assert "within US" in r.stderr


# --- antimeridian ---------------------------------------------------------


def test_a_wrapped_box_contains_both_sides_of_the_dateline():
    """Fiji is 177E..-179E. `west <= lon <= east` is false for every longitude
    on Earth there, so the layer was skipped and the query came back empty."""
    for lon in (177.0, 179.9, -180.0, -179.0):
        assert lon_within(177.0, -179.0, lon), lon
    for lon in (0.0, 100.0, 170.0, -170.0):
        assert not lon_within(177.0, -179.0, lon), lon


def test_an_ordinary_box_is_unaffected():
    assert lon_within(130.0, 150.0, 139.7)
    assert not lon_within(130.0, 150.0, 100.0)
    assert lon_within(130.0, 150.0, 129.99, pad=0.05)


def test_range_overlap_handles_either_side_wrapping():
    assert lon_ranges_overlap(177.0, -179.0, -179.5, -179.2)  # wrapped vs east
    assert lon_ranges_overlap(177.0, -179.0, 177.2, 178.0)    # wrapped vs west
    assert not lon_ranges_overlap(177.0, -179.0, 0.0, 10.0)
    assert lon_ranges_overlap(130.0, 150.0, 140.0, 160.0)
    assert not lon_ranges_overlap(130.0, 150.0, 0.0, 10.0)
    assert lon_ranges_overlap(177.0, -179.0, 170.0, -170.0)   # both wrap


def test_the_build_side_filter_wraps_too():
    """A wrapped bbox in the region table would otherwise drop every feature."""
    from build_state_v8 import _lon_within

    assert _lon_within(177.0, -179.0, 179.5)
    assert _lon_within(177.0, -179.0, -179.5)
    assert not _lon_within(177.0, -179.0, 0.0)
    assert _lon_within(130.0, 150.0, 139.7)


# --- lazy loading ---------------------------------------------------------


@pytest.fixture
def jp_snapshot(tmp_path):
    if not JP_KANTO.exists():
        pytest.skip("Japan build not present")
    (tmp_path / "JP").mkdir()
    for f in (SNAPSHOT / "v4/states").glob("JP-*.buildings_v9.ptiles"):
        (tmp_path / "JP" / f.name).symlink_to(f)
    for f in (SNAPSHOT / "states").glob("JP.*.ptiles"):
        (tmp_path / "JP" / f.name).symlink_to(f)
    return tmp_path


def test_opening_a_country_decodes_no_indexes(jp_snapshot):
    """Opening is what a client does for every country it might query; decoding
    is what it should only do for the ones it actually does."""
    from ptiles.composite import PtilesClient

    c = PtilesClient.open_country("JP", jp_snapshot)
    assert len(c._layers) >= 10
    eager = [l.scope for l in c._layers if getattr(l._reader, "index_loaded", False)]
    assert eager == [], f"these decoded their index at open: {eager}"


def test_querying_decodes_only_the_covering_files(jp_snapshot):
    from ptiles.composite import PtilesClient

    c = PtilesClient.open_country("JP", jp_snapshot)
    c.query_point(35.6812, 139.7671, include_sun=False)  # Tokyo
    decoded = sorted(
        l.scope for l in c._building_layers if l._reader.index_loaded
    )
    assert "JP-KANTO" in decoded
    assert len(decoded) < 8, f"decoded {decoded}, expected only the covering ones"


# --- PTLR bounds ----------------------------------------------------------


def test_ptlr_header_carries_bounds(tmp_path):
    """Without these the manifest reports null and every client reads every
    roads file on every query."""
    src = SNAPSHOT / "JP.roads.ptiles"
    if not src.exists():
        pytest.skip("no roads file")
    with open(src, "rb") as f:
        head = f.read(256)
    assert head[:4] == b"PTLR"
    mnx, mny, mxx, mxy = struct.unpack_from("<iiii", head, 84)
    if (mnx, mny, mxx, mxy) == (0, 0, 0, 0):
        pytest.skip("file predates the bounds field (reported as null, by design)")
    assert mnx < mxx and mny < mxy
    assert 100 < mnx / 1e5 < 160, "Japan should sit in the western Pacific"
