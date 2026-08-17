"""Multi-country scope handling: naming, publish layout, and multi-file layers.

The regression these guard against is subtle. A country whose layers are split
across region files (Japan's buildings are eight, because 29.5M will not fit one
build) puts several readers behind one layer. The adapters all wrote their answer
into a shared report with `=`, so a region that legitimately covers the query
point but contains no feature there wrote its empty answer over a real hit from
another region -- and which one won depended on filename order.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ptiles.scopes import collides_with_us_state, country_of, publish_relpath  # noqa: E402

SNAPSHOT = Path("/mnt/core/kino/ptiles/data")
JP_BUILDINGS = SNAPSHOT / "v4/states/JP-KANTO.buildings_v9.ptiles"


# --- naming ---------------------------------------------------------------


def test_country_of():
    assert country_of("TN") == "US"  # a state
    assert country_of("US") == "US"
    assert country_of("JP") == "JP"  # a country
    assert country_of("JP-KANTO") == "JP"  # a subdivision


def test_bare_state_code_beats_country_code():
    """`DE` is Delaware here, because the US set owns the unprefixed namespace.

    Germany must therefore be published as alpha-3 (`DEU`) or by subdivision
    (`DE-BY`); see tests/test_scaling_fixes.py for the guard that enforces it.
    """
    assert country_of("DE") == "US"
    assert country_of("DE-BY") == "DE"
    assert country_of("DEU") == "DEU"
    assert collides_with_us_state("DE")


# `JPN` is valid now -- three letters is the ISO alpha-3 form, used where a
# country's alpha-2 is already a US state abbreviation.
@pytest.mark.parametrize("bad", ["", "jp", "JAPN", "JP_KANTO", "TN.roads"])
def test_malformed_scope_rejected(bad):
    with pytest.raises(ValueError):
        country_of(bad)


def test_publish_relpath_keeps_us_at_root():
    """US URLs are already published and read by clients outside this repo."""
    assert publish_relpath("TN", "TN.buildings_v9.ptiles") == "TN.buildings_v9.ptiles"
    assert (
        publish_relpath("JP-KANTO", "JP-KANTO.buildings_v9.ptiles")
        == "JP/JP-KANTO.buildings_v9.ptiles"
    )


# --- manifest -------------------------------------------------------------


def _run_manifest(build_dir):
    return subprocess.run(
        [sys.executable, str(REPO / "scripts/gen_manifest.py"), str(build_dir),
         "2026-08-20", "test"],
        capture_output=True, text=True,
    )


@pytest.mark.skipif(not JP_BUILDINGS.exists(), reason="Japan build not present")
def test_manifest_includes_subdivision_scopes(tmp_path):
    """`[A-Z]{2}` silently dropped every JP-<REGION> file from the manifest."""
    (tmp_path / "JP-KANTO.buildings_v9.ptiles").symlink_to(JP_BUILDINGS)
    r = _run_manifest(tmp_path)
    assert r.returncode == 0, r.stderr
    m = json.loads(r.stdout)
    scopes = m["layers"]["buildings"]["scopes"]
    assert "JP-KANTO" in scopes
    assert scopes["JP-KANTO"]["country"] == "JP"
    assert scopes["JP-KANTO"]["path"] == "JP/JP-KANTO.buildings_v9.ptiles"
    assert m["countries"]["JP"] == ["JP-KANTO"]


@pytest.mark.skipif(not JP_BUILDINGS.exists(), reason="Japan build not present")
def test_manifest_fatal_on_unrecognised_name(tmp_path):
    """Skipping a file silently publishes a snapshot missing a layer."""
    (tmp_path / "not-a-scope.ptiles").symlink_to(JP_BUILDINGS)
    r = _run_manifest(tmp_path)
    assert r.returncode != 0
    assert "unrecognised" in r.stderr


@pytest.mark.skipif(
    not (SNAPSHOT / "JP.roads.ptiles").exists(), reason="Japan roads not present"
)
def test_manifest_reports_ptlr_without_inventing_bounds(tmp_path):
    """PTLR is not the PTILES container, and read_header does not reject it --
    it reads PTLR's bytes through the PTILES field layout, which yielded a
    feature count of 471450137651052544 and bounds of ~1e-39 for every roads
    file published. Bounds must be either genuinely recorded or null, never
    that garbage.
    """
    (tmp_path / "JP.roads.ptiles").symlink_to(SNAPSHOT / "JP.roads.ptiles")
    r = _run_manifest(tmp_path)
    assert r.returncode == 0, r.stderr
    entry = json.loads(r.stdout)["layers"]["roads"]["scopes"]["JP"]
    assert entry["format"] == "PTLR"
    assert entry["features"] == 10_552_202

    bounds = entry["bounds"]
    if bounds is None:
        return  # built before the bbox field; unknown is the honest answer
    min_lat, min_lon, max_lat, max_lon = bounds
    assert min_lat < max_lat and min_lon < max_lon
    assert 20 < min_lat < 46 and 122 < min_lon < 155, "Japan, not a rounding artefact"


# --- client ---------------------------------------------------------------


@pytest.fixture(scope="module")
def jp_snapshot(tmp_path_factory):
    """A published-shaped tree: US at the root, other countries in a directory."""
    if not JP_BUILDINGS.exists():
        pytest.skip("Japan build not present")
    root = tmp_path_factory.mktemp("snap")
    (root / "JP").mkdir()
    for f in (SNAPSHOT / "v4/states").glob("JP-*.buildings_v9.ptiles"):
        (root / "JP" / f.name).symlink_to(f)
    for f in (SNAPSHOT / "states").glob("JP.*.ptiles"):
        (root / "JP" / f.name).symlink_to(f)
    return root


def test_discover_scopes(jp_snapshot):
    from ptiles.composite import PtilesClient

    scopes = PtilesClient.discover_scopes(jp_snapshot, "JP")
    assert "JP" in scopes and "JP-KANTO" in scopes
    assert len(scopes) == 9  # country-wide plus 8 regions


def test_open_country_holds_many_files_for_one_layer(jp_snapshot):
    from ptiles.composite import PtilesClient

    c = PtilesClient.open_country("JP", jp_snapshot)
    assert len(c._building_layers) == 8
    # Country-wide layers load once, not once per region.
    from ptiles.composite import PlaceLayer

    assert sum(isinstance(l, PlaceLayer) for l in c._layers) == 1


def test_point_query_consults_only_covering_files(jp_snapshot):
    from ptiles.composite import PtilesClient

    c = PtilesClient.open_country("JP", jp_snapshot)
    lat, lon = 35.6812, 139.7671  # Tokyo Station
    consulted = [l.scope for l in c._building_layers if l.covers(lat, lon, pad=0.05)]
    assert "JP-KANTO" in consulted
    assert len(consulted) < 8
    r = c.query_point(lat, lon, include_sun=False)
    assert r.building is not None
    assert r.places, "Chiyoda should resolve places"


def test_seam_point_keeps_the_hit(jp_snapshot):
    """A covering-but-empty region must not erase another region's hit.

    36.986N,140.0E sits in the Kanto/Tohoku overlap. Kanto has the building;
    Tohoku covers the point, finds nothing, and sorts last -- so under the
    previous `report.building = ...` this returned None.
    """
    from ptiles.composite import PtilesClient

    c = PtilesClient.open_country("JP", jp_snapshot)
    lat, lon = 36.986, 140.0
    covering = {l.scope: l._reader.query(lat, lon) for l in c._building_layers
                if l.covers(lat, lon, pad=0.05)}
    assert covering.get("JP-KANTO") is not None, "expected a Kanto building here"
    assert covering.get("JP-TOHOKU") is None, "expected Tohoku to cover but miss"
    assert max(covering) == "JP-TOHOKU", "the miss must sort last for this to bite"

    assert c.query_point(lat, lon, include_sun=False).building is not None


def test_from_manifest_opens_declared_files(jp_snapshot, tmp_path):
    from ptiles.composite import PtilesClient

    r = _run_manifest(jp_snapshot / "JP")
    assert r.returncode == 0, r.stderr
    manifest = json.loads(r.stdout)
    # The manifest paths are country-relative; the JP dir is the country root.
    c = PtilesClient.from_manifest(manifest, jp_snapshot, country="JP")
    assert c._layers, "manifest-driven open found nothing"
    assert len(c._building_layers) == 8
