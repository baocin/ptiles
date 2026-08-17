"""Alternative names: name:en and brand, per layer.

A third of named OSM nodes in Japan carry name:en and no builder read it, so
cross-language search had nothing to match on -- 居酒屋 could never find "pub".
Every layer now stores it where the source has it.

Two encodings, because the layers differ:

* buildings appends behind flags2 0x80 -> flags3, no version bump. Its records
  are length-prefixed, so an older reader walks past unknown trailing fields.
* everything else field-walks, where an appended field desyncs the rest of the
  cell. Those layers went v1 -> v2, and a v1 file is still readable because the
  new fields are flag-guarded and a v1 file has the bits clear.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

DATA = Path("/mnt/core/kino/ptiles/data")


def newest(pattern: str):
    found = sorted(DATA.glob(pattern))
    return found[-1] if found else None


# --- buildings: appended, no version bump ---------------------------------


BUILDINGS = DATA / "v4/states/JP-SHIKOKU.buildings_v9.ptiles"


@pytest.mark.skipif(not BUILDINGS.exists(), reason="Shikoku buildings not built")
def test_buildings_carry_alt_names():
    from ptiles.buildings import BuildingsReader

    r = BuildingsReader.open(BUILDINGS)
    named = with_en = 0
    pairs = []
    for entry in r._index[:800]:
        for b in r.get_in_cell(entry["h3_cell"]):
            if b.name:
                named += 1
            if b.name_en:
                with_en += 1
                if len(pairs) < 3:
                    pairs.append((b.name, b.name_en))
    assert named, "expected named buildings in Shikoku"
    assert with_en, "expected some name:en"
    # The pairing has to be real, not a mis-slice: a Japanese primary name with
    # a Latin-script English one.
    assert any(
        n and e and n != e and e.isascii() and not n.isascii() for n, e in pairs
    ), pairs


@pytest.mark.skipif(
    not (DATA / "v4/states/JP-KANTO.buildings_v9.ptiles").exists(),
    reason="no pre-change buildings file",
)
def test_a_buildings_file_written_before_flags3_still_decodes():
    """flags2 0x80 clear means no flags3 byte, and no phantom names."""
    from ptiles.buildings import BuildingsReader

    r = BuildingsReader.open(DATA / "v4/states/JP-KANTO.buildings_v9.ptiles")
    decoded = phantom = 0
    for entry in r._index[:200]:
        for b in r.get_in_cell(entry["h3_cell"]):
            decoded += 1
            if b.name_en or b.brand or b.alt_name:
                phantom += 1
    assert decoded > 1000
    assert phantom == 0, "a pre-change file cannot have alternative names"


# --- the v2 layers --------------------------------------------------------


V2_LAYERS = [
    ("places", "ptiles.places", "PlacesReader"),
    ("parks", "ptiles.parks", "ParkReader"),
    ("rail", "ptiles.rail", "RailReader"),
    ("trails", "ptiles.trails", "TrailsReader"),
    ("ev", "ptiles.ev", "EvReader"),
]


@pytest.mark.parametrize("layer,module,cls_name", V2_LAYERS)
def test_v2_layers_declare_version_2(layer, module, cls_name):
    """The bump is the contract: these records are not length-prefixed, so a v1
    reader pointed at a v2 file would desync rather than skip."""
    import importlib

    path = newest(f"states/JP-SHIKOKU.{layer}_v*.ptiles")
    if path is None:
        pytest.skip(f"{layer} not built for Shikoku")
    reader = getattr(importlib.import_module(module), cls_name).open(path)
    assert path.name.endswith("_v2.ptiles"), path.name
    assert reader.header["version"] == 2


@pytest.mark.parametrize("layer,module,cls_name", V2_LAYERS)
def test_v2_records_decode_cleanly(layer, module, cls_name):
    """A drifted field would desync the cell and truncate it, so the count is
    the check that every field width is right."""
    import importlib

    path = newest(f"states/JP-SHIKOKU.{layer}_v*.ptiles")
    if path is None:
        pytest.skip(f"{layer} not built for Shikoku")
    reader = getattr(importlib.import_module(module), cls_name).open(path)
    total = sum(len(reader.get_in_cell(e["h3_cell"])) for e in reader._index)
    assert total == reader.header["feature_count"], (
        f"{layer}: decoded {total}, header says {reader.header['feature_count']}"
    )


def test_rail_has_english_names_for_most_lines():
    """Rail is the best-covered layer: Japanese lines almost all carry name:en,
    which is what makes 'Naruto Line' findable from JR鳴門線."""
    from ptiles.rail import RailReader

    path = newest("states/JP-SHIKOKU.rail_v*.ptiles")
    if path is None:
        pytest.skip("rail not built")
    r = RailReader.open(path)
    named = with_en = 0
    for e in r._index:
        for x in r.get_in_cell(e["h3_cell"]):
            if x.name:
                named += 1
            if x.name_en:
                with_en += 1
    assert named
    assert with_en / named > 0.5, f"only {with_en}/{named} rail names had name:en"


def test_places_alt_names_are_populated():
    from ptiles.places import PlacesReader

    path = newest("states/JP-SHIKOKU.places_v*.ptiles")
    if path is None:
        pytest.skip("places not built")
    r = PlacesReader.open(path)
    total = with_en = 0
    for e in r._index:
        for p in r.get_in_cell(e["h3_cell"]):
            total += 1
            if p.name_en:
                with_en += 1
    assert total
    # Place nodes are far better covered than buildings; anything near zero
    # means the field is not being read from OSM at all.
    assert with_en / total > 0.2, f"{with_en}/{total} places had name:en"


def test_every_layer_exposes_the_new_fields():
    from ptiles.buildings import Building
    from ptiles.ev import Charger
    from ptiles.parks import ParkFeature
    from ptiles.places import Place
    from ptiles.rail import RailFeature
    from ptiles.trails import Trail

    for cls in (Building, Charger, ParkFeature, Place, RailFeature, Trail):
        fields = cls.__dataclass_fields__
        assert "name_en" in fields, cls.__name__
        assert "brand" in fields, cls.__name__


def test_client_finds_v2_files():
    """LAYER_FILE_ALIASES has to know the new suffixes or the client silently
    opens nothing for these layers."""
    from ptiles.composite import LAYER_FILE_ALIASES

    for layer in ("places", "parks", "rail", "trails", "ev"):
        aliases = LAYER_FILE_ALIASES[layer]
        assert aliases[0] == f"{layer}_v2", aliases
        assert f"{layer}_v1" in aliases, aliases


# --- trails -> parks ------------------------------------------------------


def test_trail_park_ids_agree_with_the_parks_layer():
    """The stored id must match what a runtime point-in-polygon would find.

    It did not at first: the build tested the raw OSM coordinate while the file
    stores it quantised to 1e5, so a trail starting within ~1 m of a park edge
    landed inside at build time and outside at read time -- 27 of 3739
    disagreed. The build now tests the coordinate it is about to write.
    """
    from ptiles.geometry import point_in_polygon
    from ptiles.parks import ParkReader
    from ptiles.trails import TrailsReader

    tp = newest("states/JP-SHIKOKU.trails_v*.ptiles")
    pp = newest("states/JP-SHIKOKU.parks_v*.ptiles")
    if tp is None or pp is None:
        pytest.skip("trails or parks not built for Shikoku")

    parks = {
        p.osm_id: tuple(p.coords)
        for e in ParkReader.open(pp)._index
        for p in ParkReader.open(pp).get_in_cell(e["h3_cell"])
        if p.coords
    }
    reader = TrailsReader.open(tp)
    tagged = confirmed = 0
    for entry in reader._index:
        for t in reader.get_in_cell(entry["h3_cell"]):
            if not t.park_osm_id:
                continue
            tagged += 1
            ring = parks.get(t.park_osm_id)
            if ring and point_in_polygon(t.coords[0][0], t.coords[0][1], ring):
                confirmed += 1
    assert tagged, "expected some trails to start inside a park"
    assert confirmed == tagged, f"{tagged - confirmed} of {tagged} did not agree"


def test_most_trails_are_not_in_a_park():
    """Sanity on the join: a footpath is usually just a footpath. If everything
    or nothing were tagged, the containment test would be the suspect."""
    from ptiles.trails import TrailsReader

    tp = newest("states/JP-SHIKOKU.trails_v*.ptiles")
    if tp is None:
        pytest.skip("trails not built")
    reader = TrailsReader.open(tp)
    total = tagged = 0
    for entry in reader._index:
        for t in reader.get_in_cell(entry["h3_cell"]):
            total += 1
            if t.park_osm_id:
                tagged += 1
    assert total
    assert 0 < tagged < total * 0.5, f"{tagged} of {total} trails tagged"
