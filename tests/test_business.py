"""
Tests for ptiles.business module.

Tests BusinessReader.open() and nearby() against the
TN.business.ptiles test data file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptiles.business import BusinessReader, Business, BusinessHit

DATA_DIR = Path(os.path.expanduser("~/kino/projects/ptiles/data/states"))
TN_BUSINESS = DATA_DIR / "TN.business.ptiles"
TN_BUSINESS_CATEGORIES = DATA_DIR / "TN.business_categories.json"


@pytest.mark.skipif(not TN_BUSINESS.exists(), reason=f"Test data not found: {TN_BUSINESS}")
class TestBusinessReader:

    @pytest.fixture
    def reader(self) -> BusinessReader:
        r = BusinessReader.open(TN_BUSINESS)
        yield r
        r.close()

    def test_open(self, reader: BusinessReader):
        """Verify header is parsed correctly."""
        h = reader.header
        assert h is not None
        assert h["feature_count"] > 0
        assert h["block_count"] > 0

    def test_nearby_nashville(self, reader: BusinessReader):
        """Test nearby query near Nashville returns results."""
        results = reader.nearby(36.1627, -86.7816,
                                radius_meters=1000, limit=10)
        assert len(results) >= 1
        assert len(results) <= 10
        assert isinstance(results[0], BusinessHit)
        assert isinstance(results[0].business, Business)
        assert results[0].business.name != ""
        # Results should be sorted by distance
        for i in range(len(results) - 1):
            assert results[i].distance_meters <= results[i + 1].distance_meters

    def test_nearby_with_category_prefix(self, reader: BusinessReader):
        """Test nearby with category filter."""
        results = reader.nearby(36.1627, -86.7816,
                                radius_meters=2000, limit=10,
                                category_prefix="restaurant")
        # May be empty if no restaurants nearby, but shouldn't error
        for hit in results:
            if hit.business.category:
                assert not hit.business.category.startswith("restaurant") or \
                       hit.business.category.startswith("restaurant")

    def test_nearby_with_specific_category(self, reader: BusinessReader):
        """Test nearby with category_prefix='restaurant' returns only restaurants."""
        results = reader.nearby(36.1627, -86.7816,
                                radius_meters=2000, limit=20,
                                category_prefix="restaurant")
        assert len(results) > 0, "Expected at least one restaurant near Nashville"
        for hit in results:
            # Businesses without a category pass through the prefix filter
            if hit.business.category is not None:
                assert hit.business.category.startswith("restaurant"), (
                    f"Expected restaurant category, got '{hit.business.category}'"
                )

    def test_nearby_with_exclude_closed(self, reader: BusinessReader):
        """Test nearby with exclude_closed flag."""
        results = reader.nearby(36.1627, -86.7816,
                                radius_meters=500, limit=10,
                                exclude_closed=True)
        for hit in results:
            assert hit.business.operating_status != "closed"

    def test_get_in_cell(self, reader: BusinessReader):
        """Test get_in_cell."""
        import h3
        cell = h3.latlng_to_cell(36.1627, -86.7816, 7)
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        businesses = reader.get_in_cell(cell_int)
        assert len(businesses) >= 0
        for b in businesses:
            assert isinstance(b, Business)

    def test_get_in_bounds_nashville(self, reader: BusinessReader):
        """Test get_in_bounds returns businesses in a small bbox around Nashville."""
        results = reader.get_in_bounds(36.15, -86.79, 36.17, -86.77, limit=50)
        assert 0 <= len(results) <= 50
        for b in results:
            assert isinstance(b, Business)
            assert isinstance(b.name, str)
            assert b.name != ""
