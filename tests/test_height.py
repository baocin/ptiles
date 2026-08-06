"""
Building height: encoding boundaries and reader behaviour.

Height is stored two ways in the same record, and they disagree about
precision on purpose:

  * `height_tier`  2 bits in the main flags byte, always present
  * `height_m`     u8 in 0.5 m steps behind flags2 & 0x10, optional

Both derive from one source value, so the interesting cases are the edges of
the u8 quantisation — 0.5 m resolution, a ceiling at 127.5 m — and the tier
thresholds, where a building exactly on a boundary must land on a defined
side. The reader dropped both fields entirely until recently, so "is it read
at all" is itself worth pinning.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ptiles.buildings import HEIGHT_TIERS, Building  # noqa: E402

encode_v8 = pytest.importorskip("encode_v8", reason="scripts/ not importable")


class TestHeightParsing:
    """parse_height accepts what OSM actually contains, and nothing else."""

    @pytest.mark.parametrize("raw,expected", [
        ("12", 12.0),
        ("12.5", 12.5),
        ("0.5", 0.5),
        ("12 m", 12.0),
        ("12m", 12.0),
        ("12 meters", 12.0),
        ("40 ft", 12.192),
        ("40ft", 12.192),
        (7.5, 7.5),
        (12, 12.0),
    ])
    def test_accepted(self, raw, expected):
        assert encode_v8.parse_height(raw) == pytest.approx(expected, abs=0.001)

    @pytest.mark.parametrize("raw", [
        None, "", "   ", "abc", "tall", "-3", "0", "1e9", "5000",
    ])
    def test_rejected(self, raw):
        # A rejected height is None, never 0.0 — the difference between
        # "unknown" and "flat" matters to anything casting a shadow.
        assert encode_v8.parse_height(raw) is None

    def test_zero_is_not_a_height(self):
        assert encode_v8.parse_height("0") is None

    def test_absurd_height_rejected(self):
        # Taller than any building; almost certainly a units error in OSM.
        assert encode_v8.parse_height("5000") is None

    def test_just_under_the_sanity_ceiling(self):
        assert encode_v8.parse_height("999") == pytest.approx(999.0)


class TestLevelsParsing:
    @pytest.mark.parametrize("raw,expected", [("1", 1.0), ("4", 4.0), ("2.5", 2.5)])
    def test_accepted(self, raw, expected):
        assert encode_v8.parse_levels(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "abc", "0", "-2", "500"])
    def test_rejected(self, raw):
        assert encode_v8.parse_levels(raw) is None

    def test_levels_convert_at_the_documented_rate(self):
        assert encode_v8.parse_levels("4") * encode_v8.METERS_PER_LEVEL == pytest.approx(12.8)


class TestHeightTier:
    """Tier thresholds must be closed on one side, with no gap or overlap."""

    @pytest.mark.parametrize("height,tier", [
        (None, 0), (0, 0), (-1, 0),
        (0.5, 1), (3.0, 1), (6.0, 1),      # <= 6 m
        (6.01, 2), (10.0, 2), (15.0, 2),   # <= 15 m
        (15.01, 3), (50.0, 3), (300.0, 3),
    ])
    def test_classification(self, height, tier):
        assert encode_v8.classify_height_tier(height) == tier

    def test_boundaries_belong_to_the_lower_tier(self):
        assert encode_v8.classify_height_tier(6.0) == 1
        assert encode_v8.classify_height_tier(6.0001) == 2
        assert encode_v8.classify_height_tier(15.0) == 2
        assert encode_v8.classify_height_tier(15.0001) == 3

    def test_every_tier_has_a_name(self):
        for tier in (0, 1, 2, 3):
            assert tier in HEIGHT_TIERS
        assert HEIGHT_TIERS[0] == "unknown"


class TestU8Quantisation:
    """height_m is a u8 in 0.5 m steps: 0.5 m resolution, 127.5 m ceiling."""

    @staticmethod
    def _round_trip(height_m: float) -> float:
        raw = min(255, round(height_m * 2))
        return raw * 0.5

    @pytest.mark.parametrize("height", [0.5, 1.0, 3.5, 10.0, 87.5, 127.5])
    def test_exact_multiples_survive(self, height):
        assert self._round_trip(height) == height

    def test_half_metre_resolution(self):
        # 3.7 m cannot be represented; it lands on the nearer step.
        assert self._round_trip(3.7) == 3.5
        assert self._round_trip(3.8) == 4.0

    def test_ceiling_saturates_silently(self):
        # Anything above 127.5 m is clamped, so a skyscraper reads as 127.5.
        # This is a real limit of the format, not a reader bug: DC's tallest
        # decoded height is exactly 127.5.
        assert self._round_trip(200.0) == 127.5
        assert self._round_trip(1000.0) == 127.5

    def test_the_ceiling_is_reachable_exactly(self):
        assert self._round_trip(127.5) == 127.5
        assert min(255, round(127.5 * 2)) == 255


class TestBuildingDefaults:
    def test_height_absent_is_none_not_zero(self):
        b = Building(osm_id=1, building_type="yes", centroid_lat=0.0,
                     centroid_lon=0.0, coordinates=())
        assert b.height_m is None
        assert b.height_tier == "unknown"

    def test_height_is_carried_when_present(self):
        b = Building(osm_id=1, building_type="yes", centroid_lat=0.0,
                     centroid_lon=0.0, coordinates=(), height_m=12.5,
                     height_tier="3-5")
        assert b.height_m == 12.5
        assert b.height_tier == "3-5"


DC = Path(os.environ.get(
    "PTILES_BUILDINGS", "/nonexistent/DC.buildings_v9.ptiles"))


@pytest.mark.skipif(not DC.exists(), reason=f"no buildings fixture at {DC}")
class TestAgainstRealFile:
    """Coverage is the headline number here, so it is asserted, not assumed."""

    @pytest.fixture(scope="class")
    @classmethod
    def heights(cls):
        from ptiles.buildings import BuildingsReader
        r = BuildingsReader.open(DC)
        try:
            out = []
            for e in r._index:
                out.extend(b.height_m for b in r.get_in_cell(e["h3_cell"]))
            yield out
        finally:
            r.close()

    def test_every_height_is_a_positive_half_metre_multiple(self, heights):
        for h in (x for x in heights if x is not None):
            assert h > 0, "zero should be encoded as absent, not 0.0"
            assert (h * 2) == int(h * 2), f"{h} is not a 0.5 m step"

    def test_no_height_exceeds_the_u8_ceiling(self, heights):
        present = [h for h in heights if h is not None]
        assert present, "fixture has no heights at all"
        assert max(present) <= 127.5

    def test_absent_heights_are_none(self, heights):
        # The failure mode worth catching is a reader that invents 0.0.
        assert all(h is None or h > 0 for h in heights)
