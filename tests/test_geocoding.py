"""
Offline geocoding: forward, reverse, and the boundaries either can hit.

Both directions rest on the address layer carrying per-record coordinates,
which v1 does not. The most important behaviour here is therefore negative:
`reverse` must refuse a v1 file rather than rank addresses that all share one
position, and a v1 result must announce its own imprecision. A geocoder that
answers confidently from a cell centre is worse than one that declines.

Set PTILES_ADDRESS_V2 / PTILES_ADDRESS_V1 to run the fixture-backed cases.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptiles.address import Address, AddressReader
from ptiles.geometry import haversine_meters

V2 = Path(os.environ.get("PTILES_ADDRESS_V2", "/nonexistent/v2.ptiles"))
V1 = Path(os.environ.get("PTILES_ADDRESS_V1", "/nonexistent/v1.ptiles"))

# Downtown Nashville, inside the TN fixture's coverage.
NASHVILLE = (36.1627, -86.7816)


class TestAddressFormatting:
    def test_number_and_street(self):
        assert Address(1, "123", "Main St", 0, 0).formatted == "123 Main St"

    def test_missing_street(self):
        assert Address(1, "123", "", 0, 0).formatted == "123"

    def test_missing_number(self):
        assert Address(1, "", "Main St", 0, 0).formatted == "Main St"

    def test_both_missing(self):
        assert Address(1, "", "", 0, 0).formatted == ""


@pytest.mark.skipif(not V2.exists(), reason=f"no v2 fixture at {V2}")
class TestForward:
    @pytest.fixture(scope="class")
    @classmethod
    def reader(cls):
        r = AddressReader.open(V2)
        yield r
        r.close()

    def test_finds_a_known_street(self, reader):
        hits = reader.geocode("Church Street", near=NASHVILLE, limit=5)
        assert hits
        assert all("church street" in a.street.casefold() for a in hits)

    def test_results_carry_usable_coordinates(self, reader):
        for a in reader.geocode("Church Street", near=NASHVILLE, limit=3):
            assert a.coords_exact
            assert -90 <= a.lat <= 90
            assert -180 <= a.lon <= 180
            assert abs(a.lat) > 0.001 and abs(a.lon) > 0.001

    def test_house_number_narrows(self, reader):
        broad = reader.geocode("Church Street", near=NASHVILLE, limit=50)
        assert broad
        number = broad[0].housenumber
        narrow = reader.geocode("Church Street", number, near=NASHVILLE, limit=50)
        assert narrow
        assert all(a.housenumber == number for a in narrow)
        assert len(narrow) <= len(broad)

    def test_street_match_is_case_insensitive(self, reader):
        lower = reader.geocode("church street", near=NASHVILLE, limit=3)
        upper = reader.geocode("CHURCH STREET", near=NASHVILLE, limit=3)
        assert lower and upper
        assert {a.osm_id for a in lower} == {a.osm_id for a in upper}

    def test_house_number_match_is_case_insensitive(self, reader):
        # House numbers carry letters: "12A", "7b".
        hits = reader.geocode("Church Street", near=NASHVILLE, limit=50)
        num = hits[0].housenumber
        assert reader.geocode("Church Street", num.lower(), near=NASHVILLE, limit=5)
        assert reader.geocode("Church Street", num.upper(), near=NASHVILLE, limit=5)

    def test_limit_is_honoured(self, reader):
        assert len(reader.geocode("Street", near=NASHVILLE, limit=3)) <= 3

    def test_unknown_street_returns_empty_not_error(self, reader):
        assert reader.geocode("Nonexistent Boulevard Of Nowhere",
                              near=NASHVILLE, limit=5) == []

    def test_empty_street_matches_broadly_rather_than_crashing(self, reader):
        # "" is a substring of everything; the contract is that it does not
        # raise, not that it is useful.
        assert isinstance(reader.geocode("", near=NASHVILLE, limit=2), list)

    def test_near_far_from_any_data_returns_empty(self, reader):
        # Mid-Pacific: the spiral must terminate rather than walk the state.
        assert reader.geocode("Church Street", near=(0.0, -150.0), limit=3) == []

    def test_results_are_ordered_by_distance_from_near(self, reader):
        hits = reader.geocode("Street", near=NASHVILLE, limit=8)
        d = [haversine_meters(*NASHVILLE, a.lat, a.lon) for a in hits]
        assert d == sorted(d)


@pytest.mark.skipif(not V2.exists(), reason=f"no v2 fixture at {V2}")
class TestReverse:
    @pytest.fixture(scope="class")
    @classmethod
    def reader(cls):
        r = AddressReader.open(V2)
        yield r
        r.close()

    def test_finds_something_downtown(self, reader):
        hits = reader.reverse(*NASHVILLE, n=3, max_meters=500)
        assert hits
        assert all(isinstance(a, Address) for a, _ in hits)

    def test_ordered_nearest_first(self, reader):
        d = [dist for _, dist in reader.reverse(*NASHVILLE, n=5, max_meters=800)]
        assert d == sorted(d)

    def test_respects_max_meters(self, reader):
        for _, dist in reader.reverse(*NASHVILLE, n=10, max_meters=250):
            assert dist <= 250

    def test_zero_radius_finds_only_an_exact_hit(self, reader):
        target = reader.reverse(*NASHVILLE, n=1, max_meters=500)[0][0]
        hits = reader.reverse(target.lat, target.lon, n=1, max_meters=0)
        assert not hits or hits[0][1] == 0.0

    def test_open_ocean_returns_empty(self, reader):
        assert reader.reverse(0.0, -150.0, n=1, max_meters=500) == []

    def test_round_trips_with_forward(self, reader):
        a = reader.geocode("Church Street", near=NASHVILLE, limit=1)[0]
        back, dist = reader.reverse(a.lat, a.lon, n=1, max_meters=50)[0]
        assert dist < 1.0
        assert back.osm_id == a.osm_id

    def test_n_larger_than_available_is_not_an_error(self, reader):
        hits = reader.reverse(*NASHVILLE, n=10_000, max_meters=100)
        assert isinstance(hits, list)

    @pytest.mark.parametrize("lat,lon", [(90.0, 0.0), (-90.0, 0.0), (0.0, 180.0), (0.0, -180.0)])
    def test_extreme_coordinates_do_not_fault(self, reader, lat, lon):
        # Nothing is there, but the H3 spiral must not throw at a pole or the
        # antimeridian.
        assert reader.reverse(lat, lon, n=1, max_meters=100) == []


@pytest.mark.skipif(not V1.exists(), reason=f"no v1 fixture at {V1}")
class TestV1RefusesToGuess:
    """v1 has no per-record position; the reader must say so, not improvise."""

    @pytest.fixture(scope="class")
    @classmethod
    def reader(cls):
        r = AddressReader.open(V1)
        yield r
        r.close()

    def test_reports_that_it_lacks_coordinates(self, reader):
        assert not reader.has_coordinates

    def test_reverse_refuses(self, reader):
        with pytest.raises(ValueError, match="address v2"):
            reader.reverse(36.0, -86.0)

    def test_forward_still_works_but_flags_imprecision(self, reader):
        hits = reader.geocode("Main", limit=3)
        if not hits:
            pytest.skip("fixture has no 'Main' street")
        for a in hits:
            # Never claims to be exact: the record carries no coordinate, the
            # position is inferred from the cell's bounding box.
            assert not a.coords_exact
            assert a.accuracy_meters is not None
            assert a.accuracy_meters >= 0

    def test_a_lone_address_in_a_cell_has_zero_uncertainty(self, reader):
        """The one case where v1 does pin a position exactly.

        The index bbox is min/max over the addresses in a cell, so a cell
        holding exactly one address has a degenerate bbox — and that point is
        the address. accuracy_meters is 0 there, correctly, while
        coords_exact stays False because the *record* still carries nothing.
        """
        singles = [e for e in reader._index if e.get("feature_count") == 1]
        if not singles:
            pytest.skip("fixture has no single-address cell")
        rows = reader.get_in_cell(singles[0]["h3_cell"])
        assert len(rows) == 1
        assert rows[0].accuracy_meters == 0.0
        assert not rows[0].coords_exact

    def test_a_crowded_cell_reports_real_uncertainty(self, reader):
        crowded = max(reader._index, key=lambda e: e.get("feature_count", 0))
        if crowded.get("feature_count", 0) < 2:
            pytest.skip("fixture has no multi-address cell")
        rows = reader.get_in_cell(crowded["h3_cell"])
        assert rows[0].accuracy_meters > 1.0

    def test_every_address_in_a_cell_shares_one_position(self, reader):
        rows = reader.get_in_cell(reader._index[0]["h3_cell"])
        assert len({(a.lat, a.lon) for a in rows}) == 1

    def test_accuracy_is_reported_in_metres_not_degrees(self, reader):
        rows = reader.get_in_cell(reader._index[0]["h3_cell"])
        # A cell is kilometres across; an accuracy under a metre would mean
        # the units were wrong.
        assert rows[0].accuracy_meters > 1.0
