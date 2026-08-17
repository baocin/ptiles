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


# Scope-agnostic on purpose. These tests were written against JP-SHIKOKU and
# silently went to 14 skips the moment those test artifacts were cleaned up --
# the same failure mode they exist to catch. Try the regional scope, then the
# country-wide one, and take whichever the build actually produced.
SCOPES = ("JP-SHIKOKU", "JP")


def newest(pattern: str):
    """Resolve `states/{scope}.{layer}_v*.ptiles` for whichever scope exists."""
    for scope in SCOPES:
        found = sorted(DATA.glob(pattern.replace("{scope}", scope)))
        if found:
            return found[-1]
    return None


# --- buildings: appended, no version bump ---------------------------------


def _buildings():
    for scope in SCOPES:
        p = DATA / f"v4/states/{scope}.buildings_v9.ptiles"
        if p.exists():
            return p
    return DATA / "v4/states/JP-SHIKOKU.buildings_v9.ptiles"


BUILDINGS = _buildings()


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
    if phantom:
        # This file has been rebuilt since, so it legitimately carries them and
        # is no longer a witness for the old layout.
        pytest.skip("file rebuilt with flags3; no longer a pre-change sample")
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

    path = newest(f"states/{{scope}}.{layer}_v*.ptiles")
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

    path = newest(f"states/{{scope}}.{layer}_v*.ptiles")
    if path is None:
        pytest.skip(f"{layer} not built for Shikoku")
    reader = getattr(importlib.import_module(module), cls_name).open(path)
    # Per-cell rather than whole-file: the index records how many features each
    # cell holds, so a desync shows up as a local mismatch just as reliably, and
    # a country-scale file no longer costs minutes. (This scanned every cell of
    # 2.2M trails and took 7 minutes on its own.)
    entries = reader._index[:: max(1, len(reader._index) // 400)]
    for entry in entries:
        got = len(reader.get_in_cell(entry["h3_cell"]))
        assert got == entry["feature_count"], (
            f"{layer}: cell {entry['h3_cell']:#x} decoded {got}, "
            f"index says {entry['feature_count']}"
        )
    assert entries, f"{layer}: empty index"


def test_rail_has_english_names_for_most_lines():
    """Rail is the best-covered layer: Japanese lines almost all carry name:en,
    which is what makes 'Naruto Line' findable from JR鳴門線."""
    from ptiles.rail import RailReader

    path = newest("states/{scope}.rail_v*.ptiles")
    if path is None:
        pytest.skip("rail not built")
    r = RailReader.open(path)
    named = with_en = 0
    for e in r._index[:: max(1, len(r._index) // 400)]:
        for x in r.get_in_cell(e["h3_cell"]):
            if x.name:
                named += 1
            if x.name_en:
                with_en += 1
    assert named
    assert with_en / named > 0.5, f"only {with_en}/{named} rail names had name:en"


def test_places_alt_names_are_populated():
    from ptiles.places import PlacesReader

    path = newest("states/{scope}.places_v*.ptiles")
    if path is None:
        pytest.skip("places not built")
    r = PlacesReader.open(path)
    total = with_en = 0
    for e in r._index[:: max(1, len(r._index) // 400)]:
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

    tp = newest("states/{scope}.trails_v*.ptiles")
    pp = newest("states/{scope}.parks_v*.ptiles")
    if tp is None or pp is None:
        pytest.skip("trails or parks not built for Shikoku")

    # Open once, not once per cell -- the comprehension used to reopen the file
    # for every index entry.
    park_reader = ParkReader.open(pp)
    parks = {
        park.osm_id: tuple(park.coords)
        for e in park_reader._index
        for park in park_reader.get_in_cell(e["h3_cell"])
        if park.coords
    }
    reader = TrailsReader.open(tp)
    tagged = confirmed = 0
    for entry in reader._index[:: max(1, len(reader._index) // 300)]:
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

    tp = newest("states/{scope}.trails_v*.ptiles")
    if tp is None:
        pytest.skip("trails not built")
    reader = TrailsReader.open(tp)
    total = tagged = 0
    for entry in reader._index[:: max(1, len(reader._index) // 300)]:
        for t in reader.get_in_cell(entry["h3_cell"]):
            total += 1
            if t.park_osm_id:
                tagged += 1
    assert total
    assert 0 < tagged < total * 0.5, f"{tagged} of {total} trails tagged"


# --- vertex-count overflow (pre-existing, found during the Japan rebuild) ---


def test_parks_vertex_count_255_round_trips():
    """255 is the escape marker, so it cannot also be a literal count.

    The encoder wrote `n if n < 256 else 255` and only emitted the u16 escape
    when `n >= 256`. At exactly 255 it therefore wrote the marker bare, the
    reader took the next two coordinate bytes as the escaped count, and the rest
    of the cell was read at the wrong offset. Two cells of Japan's 110,991 parks
    hit it -- and because build_trails reads the parks file for the park
    association, it failed the entire trails stage, not just those records.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    from build_parks import enc
    from ptiles.parks import decode_park

    for n in (254, 255, 256, 300):
        ring = [(139.0 + i * 0.0001, 35.0 + i * 0.0001) for i in range(n)]
        rec = enc({"osm_id": 1000, "park_type": "park", "coords": ring, "name": "T"}, 0)
        decoded, consumed, _ = decode_park(rec, 0, 0)
        assert consumed == len(rec), f"n={n}: consumed {consumed} of {len(rec)}"
        assert len(decoded["coords"]) == n, f"n={n}: read {len(decoded['coords'])}"


def test_parks_long_ring_fits_the_u16_escape():
    """Defence for the other end: the escape itself is u16, so a ring longer
    than 65,535 would truncate its own count. No OSM way can reach that (2,000
    node cap) but an assembled relation can."""
    sys.path.insert(0, str(REPO / "scripts"))
    from build_parks import MAX_VERTICES, _fit_vertices

    ring = [(i * 0.0001, i * 0.0001) for i in range(98_574)]
    ring.append(ring[0])
    fitted = _fit_vertices(ring)
    n = len(fitted)
    assert n <= MAX_VERTICES
    assert (((n >> 8) & 0xFF) << 8 | (n & 0xFF)) == n
    assert fitted[-1] == ring[-1], "the ring must stay closed"

    small = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]
    assert _fit_vertices(small) is small


def test_water_vertex_count_fits_its_field():
    """Same trap, same relation-assembled geometry."""
    sys.path.insert(0, str(REPO / "scripts"))
    from build_water import WATER_MAX_VERTICES, _fit_vertices

    ring = [(i * 0.0001, i * 0.0001) for i in range(200_000)]
    fitted = _fit_vertices(ring)
    assert len(fitted) <= WATER_MAX_VERTICES
    assert fitted[-1] == ring[-1]
