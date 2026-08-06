"""
Tests for ptiles.address — decoding and offline geocoding.

The v2 fixture is built by scripts/build_address.py and is not committed, so
these skip when it is absent. Point PTILES_ADDRESS_V2 at one to run them.

The v1 case matters as much as v2: those files ship without per-record
coordinates, and the reader must say so rather than present a cell centre as
an address.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptiles.address import Address, AddressReader

V2 = Path(os.environ.get("PTILES_ADDRESS_V2", "/nonexistent/TN.address_v2.ptiles"))
V1 = Path(os.environ.get("PTILES_ADDRESS_V1", "/nonexistent/TN.address.ptiles"))


class TestAddressDataclass:
    def test_formatted(self):
        a = Address(1, "123", "Main St", 36.0, -86.0)
        assert a.formatted == "123 Main St"

    def test_formatted_without_street(self):
        assert Address(1, "123", "", 36.0, -86.0).formatted == "123"

    def test_exact_by_default(self):
        a = Address(1, "1", "A", 0.0, 0.0)
        assert a.coords_exact is True
        assert a.accuracy_meters is None


@pytest.mark.skipif(not V2.exists(), reason=f"no v2 fixture at {V2}")
class TestV2:
    @pytest.fixture(scope="class")
    @classmethod
    def reader(cls):
        r = AddressReader.open(V2)
        yield r
        r.close()

    def test_header(self, reader):
        assert reader.header["magic"][:7] == b"PTILESD"
        assert reader.header["version"] >= 2
        assert reader.has_coordinates

    def test_records_carry_their_own_position(self, reader):
        cell = reader._index[0]["h3_cell"]
        rows = reader.get_in_cell(cell)
        assert rows
        assert all(a.coords_exact for a in rows)
        assert all(a.accuracy_meters is None for a in rows)
        # Distinct positions — the v1 failure mode was every row sharing one.
        assert len({(a.lat, a.lon) for a in rows}) > 1 or len(rows) == 1

    def test_decoded_count_matches_index(self, reader):
        total = sum(len(reader.get_in_cell(e["h3_cell"])) for e in reader._index[:50])
        expected = sum(e["feature_count"] for e in reader._index[:50])
        assert total == expected

    def test_reverse_returns_nearest_first(self, reader):
        hits = reader.reverse(36.1627, -86.7816, n=3, max_meters=500)
        assert hits
        dists = [d for _, d in hits]
        assert dists == sorted(dists)
        assert dists[0] < 200

    def test_forward_then_reverse_round_trips(self, reader):
        """Geocoding an address and reverse-geocoding the result must agree."""
        found = reader.geocode("Church Street", near=(36.1627, -86.7816), limit=1)
        assert found, "no Church Street near downtown Nashville"
        a = found[0]
        back = reader.reverse(a.lat, a.lon, n=1, max_meters=50)
        assert back
        assert back[0][1] < 1.0  # same point, within a metre

    def test_forward_narrows_by_housenumber(self, reader):
        both = reader.geocode("Church Street", near=(36.1627, -86.7816), limit=50)
        assert both
        num = both[0].housenumber
        narrowed = reader.geocode("Church Street", num,
                                  near=(36.1627, -86.7816), limit=50)
        assert narrowed
        assert all(a.housenumber == num for a in narrowed)

    def test_near_bounds_the_work(self, reader):
        """`near` must read far less than a full scan."""
        import time
        t0 = time.time()
        reader.geocode("Church Street", near=(36.1627, -86.7816), limit=1)
        near_s = time.time() - t0
        assert near_s < 2.0


@pytest.mark.skipif(not V1.exists(), reason=f"no v1 fixture at {V1}")
class TestV1LegacyFiles:
    @pytest.fixture(scope="class")
    @classmethod
    def reader(cls):
        r = AddressReader.open(V1)
        yield r
        r.close()

    def test_legacy_admin_magic_is_accepted(self, reader):
        # v1 shipped carrying PTILESA because a 9-byte magic was truncated.
        assert reader.header["magic"][:7] == b"PTILESA"
        assert not reader.has_coordinates

    def test_positions_are_flagged_inexact(self, reader):
        rows = reader.get_in_cell(reader._index[0]["h3_cell"])
        assert rows
        assert all(not a.coords_exact for a in rows)
        assert all(a.accuracy_meters and a.accuracy_meters > 0 for a in rows)

    def test_every_address_in_a_cell_shares_one_position(self, reader):
        """Precisely why reverse geocoding cannot work on v1."""
        rows = reader.get_in_cell(reader._index[0]["h3_cell"])
        assert len({(a.lat, a.lon) for a in rows}) == 1

    def test_reverse_refuses_rather_than_guessing(self, reader):
        with pytest.raises(ValueError, match="address v2"):
            reader.reverse(35.62, -87.86)

    def test_forward_still_works_but_is_coarse(self, reader):
        found = reader.geocode("Blue Bird Lane", limit=2)
        assert found
        assert all(not a.coords_exact for a in found)
