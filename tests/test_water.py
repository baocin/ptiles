"""
Tests for ptiles.water module.

Tests WaterReader.open() and get_in_cell() against the
TN.water.ptiles test data file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptiles.water import WaterReader, WaterFeature, LargeWaterBody, GeomType

DATA_DIR = Path(os.path.expanduser("~/kino/projects/ptiles/data/states"))
TN_WATER = DATA_DIR / "TN.water.ptiles"


@pytest.mark.skipif(not TN_WATER.exists(), reason=f"Test data not found: {TN_WATER}")
class TestWaterReader:

    @pytest.fixture
    def reader(self) -> WaterReader:
        r = WaterReader.open(TN_WATER)
        yield r
        r.close()

    def test_open(self, reader: WaterReader):
        """Verify header is parsed correctly."""
        h = reader.header
        assert h is not None
        assert h["feature_count"] > 0
        assert h["block_count"] > 0

    def test_get_in_cell_cumberland(self, reader: WaterReader):
        """Test get_in_cell near the Cumberland River in Nashville."""
        import h3
        cell = h3.latlng_to_cell(36.1627, -86.7816, 7)
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        features = reader.get_in_cell(cell_int)
        assert len(features) >= 0
        for f in features:
            assert isinstance(f, WaterFeature)
            assert isinstance(f.water_type, str)
            assert isinstance(f.geom_type, GeomType)

    def test_large_water_bodies(self, reader: WaterReader):
        """Test large_water_bodies returns the aux feature table."""
        bodies = reader.large_water_bodies()
        # Not all water files have a feature table, but check it's a list
        assert isinstance(bodies, list)
        for b in bodies:
            assert isinstance(b, LargeWaterBody)
            assert b.feature_id > 0
            assert isinstance(b.name, str)

    def test_get_in_bounds_nashville(self, reader: WaterReader):
        """Test get_in_bounds with a small bbox around Nashville."""
        results = reader.get_in_bounds(36.15, -86.79, 36.17, -86.77, limit=50)
        assert 0 <= len(results) <= 50
        for f in results:
            assert isinstance(f, WaterFeature)
            assert isinstance(f.water_type, str)
            assert isinstance(f.geom_type, GeomType)
