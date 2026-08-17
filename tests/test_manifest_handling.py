"""Manifest handling: read it when present, cope sensibly when it is not.

A published snapshot ships a manifest.json naming every scope, its layer
version, and its path. Clients should use it -- but a manifest is not
guaranteed: a partial download, a hand-assembled directory, or a snapshot
published before this format existed all have to keep working.

The distinction these tests pin down is *absent* versus *broken*. An absent
manifest is a normal condition and falls back to probing filenames. A manifest
that exists but cannot be parsed is a real fault and must raise, because
quietly probing instead would hide a corrupt publish behind a client that
merely looks like it is working.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ptiles.composite import PtilesClient, _manifest_relpath  # noqa: E402

SNAPSHOT = Path("/mnt/core/kino/ptiles/data")
JP_KANTO = SNAPSHOT / "v4/states/JP-KANTO.buildings_v9.ptiles"
JP_PLACES = SNAPSHOT / "states/JP.places_v1.ptiles"

needs_jp = pytest.mark.skipif(
    not (JP_KANTO.exists() and JP_PLACES.exists()), reason="Japan build not present"
)


@pytest.fixture
def snapshot(tmp_path):
    """A snapshot tree with two Japan files and no manifest."""
    if not (JP_KANTO.exists() and JP_PLACES.exists()):
        pytest.skip("Japan build not present")
    (tmp_path / "JP").mkdir()
    (tmp_path / "JP" / JP_KANTO.name).symlink_to(JP_KANTO)
    (tmp_path / "JP" / JP_PLACES.name).symlink_to(JP_PLACES)
    return tmp_path


def write_manifest(root: Path, obj: dict) -> Path:
    p = root / "manifest.json"
    p.write_text(json.dumps(obj))
    return p


def full_manifest() -> dict:
    return {
        "built": "2026-08-20",
        "source": "test",
        "countries": {"JP": ["JP", "JP-KANTO"]},
        "layers": {
            "buildings": {
                "version": 9,
                "pattern": "{scope}.buildings_v9.ptiles",
                "scopes": {
                    "JP-KANTO": {
                        "country": "JP",
                        "path": "JP/JP-KANTO.buildings_v9.ptiles",
                        "bounds": [24.7, 138.3, 37.2, 142.3],
                    }
                },
            },
            "places": {
                "version": 1,
                "pattern": "{scope}.places_v1.ptiles",
                "scopes": {
                    "JP": {
                        "country": "JP",
                        "path": "JP/JP.places_v1.ptiles",
                        "bounds": [20.0, 122.5, 45.9, 154.5],
                    }
                },
            },
        },
    }


# --- the manifest is there and usable ------------------------------------


@needs_jp
def test_reads_a_manifest(snapshot):
    c = PtilesClient.from_manifest(full_manifest(), snapshot, country="JP")
    assert len(c._layers) == 2
    assert {l.scope for l in c._layers} == {"JP", "JP-KANTO"}


@needs_jp
def test_accepts_a_path_as_well_as_a_dict(snapshot):
    p = write_manifest(snapshot, full_manifest())
    assert len(PtilesClient.from_manifest(p, snapshot, country="JP")._layers) == 2


@needs_jp
def test_open_country_prefers_a_manifest_when_present(snapshot):
    """The manifest names exact versions, so it wins over filename probing."""
    write_manifest(snapshot, full_manifest())
    c = PtilesClient.open_country("JP", snapshot)
    assert {l.scope for l in c._layers} == {"JP", "JP-KANTO"}


@needs_jp
def test_country_filter_excludes_other_countries(snapshot):
    m = full_manifest()
    m["countries"]["US"] = ["TN"]
    m["layers"]["buildings"]["scopes"]["TN"] = {
        "country": "US", "path": "TN.buildings_v9.ptiles", "bounds": None,
    }
    c = PtilesClient.from_manifest(m, snapshot, country="JP")
    assert all(l.scope != "TN" for l in c._layers)


@needs_jp
def test_explicit_scopes_override_the_country_index(snapshot):
    c = PtilesClient.from_manifest(full_manifest(), snapshot, scopes=["JP-KANTO"])
    assert {l.scope for l in c._layers} == {"JP-KANTO"}


# --- the manifest is missing ---------------------------------------------


@needs_jp
def test_open_country_without_a_manifest_falls_back_to_probing(snapshot):
    """An absent manifest is normal -- a partial download has no manifest."""
    assert not (snapshot / "manifest.json").exists()
    c = PtilesClient.open_country("JP", snapshot)
    assert {l.scope for l in c._layers} == {"JP", "JP-KANTO"}


def test_from_manifest_on_a_missing_path_raises_clearly(tmp_path):
    """Asking for a specific manifest that is not there is an error, not a
    silent empty client -- the caller named a file it expected to exist."""
    with pytest.raises(FileNotFoundError) as e:
        PtilesClient.from_manifest(tmp_path / "manifest.json", tmp_path)
    assert "manifest" in str(e.value)


# --- the manifest is there but broken ------------------------------------


def test_malformed_json_raises_rather_than_falling_back(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text("{not json")
    with pytest.raises(ValueError) as e:
        PtilesClient.from_manifest(p, tmp_path)
    assert "not valid JSON" in str(e.value)


def test_a_json_array_is_rejected(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text("[]")
    with pytest.raises(ValueError):
        PtilesClient.from_manifest(p, tmp_path)


def test_empty_manifest_yields_an_empty_client(tmp_path):
    """Structurally valid but describing nothing is not an error."""
    c = PtilesClient.from_manifest({}, tmp_path, country="JP")
    assert c._layers == []


def test_manifest_listing_a_missing_file_skips_it(snapshot, caplog):
    """A snapshot can be partially downloaded; the rest must still open."""
    m = full_manifest()
    m["countries"]["JP"].append("JP-KANSAI")
    m["layers"]["buildings"]["scopes"]["JP-KANSAI"] = {
        "country": "JP", "path": "JP/JP-KANSAI.buildings_v9.ptiles", "bounds": None,
    }
    with caplog.at_level("WARNING"):
        c = PtilesClient.from_manifest(m, snapshot, country="JP")
    assert {l.scope for l in c._layers} == {"JP", "JP-KANTO"}
    assert any("missing" in r.message for r in caplog.records)


@needs_jp
def test_unknown_layer_is_skipped_not_fatal(snapshot):
    """A newer snapshot may carry layers this client has no reader for."""
    m = full_manifest()
    m["layers"]["bathymetry"] = {
        "version": 1,
        "pattern": "{scope}.bathymetry_v1.ptiles",
        "scopes": {"JP": {"country": "JP", "path": "JP/JP.bathymetry_v1.ptiles"}},
    }
    c = PtilesClient.from_manifest(m, snapshot, country="JP")
    assert len(c._layers) == 2


# --- older manifests, published before countries existed -----------------


@needs_jp
def test_manifest_without_a_countries_index(snapshot):
    """Pre-countries manifests have no index; derive it from the scope names."""
    m = full_manifest()
    del m["countries"]
    c = PtilesClient.from_manifest(m, snapshot, country="JP")
    assert {l.scope for l in c._layers} == {"JP", "JP-KANTO"}


@needs_jp
def test_manifest_without_per_scope_paths(snapshot):
    """Pre-countries manifests have no `path`; rebuild it from the pattern."""
    m = full_manifest()
    for entry in m["layers"].values():
        for meta in entry["scopes"].values():
            del meta["path"]
    c = PtilesClient.from_manifest(m, snapshot, country="JP")
    assert {l.scope for l in c._layers} == {"JP", "JP-KANTO"}


def test_relpath_falls_back_through_path_then_pattern_then_version():
    entry = {"version": 9, "pattern": "{scope}.buildings_v9.ptiles"}
    assert (
        _manifest_relpath("JP-KANTO", "buildings", entry, {"path": "x/y.ptiles"})
        == "x/y.ptiles"
    )
    assert (
        _manifest_relpath("JP-KANTO", "buildings", entry, {})
        == "JP/JP-KANTO.buildings_v9.ptiles"
    )
    # No pattern either: rebuild from the layer name and version.
    assert (
        _manifest_relpath("TN", "buildings", {"version": 9}, {})
        == "TN.buildings_v9.ptiles"
    )
    # A scope that is not scope-shaped cannot be placed at all.
    assert _manifest_relpath("not-a-scope", "buildings", entry, {}) is None


# --- the publisher's view of the manifest --------------------------------


def run_publish(*args):
    import subprocess

    return subprocess.run(
        [sys.executable, str(REPO / "scripts/publish_snapshot.py"), *map(str, args)],
        capture_output=True, text=True,
    )


@needs_jp
def test_publisher_plans_us_at_root_and_countries_in_a_directory(snapshot):
    r = run_publish(snapshot, "2026-08-20", "test", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "maps/2026-08-20/JP/JP-KANTO.buildings_v9.ptiles" in r.stdout
    assert "maps/2026-08-20/manifest.json" in r.stdout
    assert "nothing uploaded" in r.stdout


@needs_jp
def test_publisher_refuses_when_manifest_and_plan_disagree(snapshot, tmp_path):
    """A file uploaded but absent from the manifest is invisible to clients."""
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({
        "built": "2026-08-20", "source": "t", "countries": {"JP": ["JP"]},
        "layers": {"places": {"version": 1, "pattern": "{scope}.places_v1.ptiles",
                              "scopes": {"JP": {"country": "JP",
                                                "path": "JP/JP.places_v1.ptiles"}}}},
    }))
    r = run_publish(snapshot, "2026-08-20", "test", "--dry-run", "--manifest", stale)
    assert r.returncode != 0
    assert "disagree" in r.stdout + r.stderr


def test_publisher_rejects_a_missing_build_dir(tmp_path):
    r = run_publish(tmp_path / "nope", "2026-08-20", "test", "--dry-run")
    assert r.returncode != 0
    assert "not a directory" in r.stdout + r.stderr


def test_publisher_rejects_an_empty_build_dir(tmp_path):
    r = run_publish(tmp_path, "2026-08-20", "test", "--dry-run")
    assert r.returncode != 0
    assert "no .ptiles" in r.stdout + r.stderr


@needs_jp
def test_flat_directory_still_works(tmp_path):
    """Manifest paths are country-qualified, but a user may have downloaded
    the files into one flat directory."""
    (tmp_path / JP_KANTO.name).symlink_to(JP_KANTO)
    m = full_manifest()
    del m["layers"]["places"]
    c = PtilesClient.from_manifest(m, tmp_path, country="JP")
    assert {l.scope for l in c._layers} == {"JP-KANTO"}
