"""Tests for error handling when opening corrupt/missing PTiles files."""

from __future__ import annotations

import os
import struct
import tempfile

import pytest

from ptiles import PTilesError, MagicError
from ptiles.buildings import BuildingsReader
from ptiles.water import WaterReader
from ptiles.business import BusinessReader
from ptiles.roads import RoadsReader


class TestMissingFile:
    """Opening a non-existent file should raise FileNotFoundError."""

    def test_buildings_missing_file(self):
        with pytest.raises(FileNotFoundError):
            BuildingsReader.open("/tmp/nonexistent_buildings.ptiles")

    def test_water_missing_file(self):
        with pytest.raises(FileNotFoundError):
            WaterReader.open("/tmp/nonexistent_water.ptiles")

    def test_business_missing_file(self):
        with pytest.raises(FileNotFoundError):
            BusinessReader.open("/tmp/nonexistent_business.ptiles")

    def test_roads_missing_file(self):
        with pytest.raises(FileNotFoundError):
            RoadsReader.open("/tmp/nonexistent_roads.ptiles")


class TestBadMagic:
    """Opening random data should raise an error."""

    @staticmethod
    def _write_temp(data: bytes) -> str:
        """Write bytes to a temp file and return the path."""
        fd, path = tempfile.mkstemp(suffix=".ptiles")
        os.write(fd, data)
        os.close(fd)
        return path

    def test_buildings_too_small(self):
        """Open a tiny file with random bytes — header read fails."""
        path = self._write_temp(b"not a ptiles file")
        try:
            with pytest.raises((ValueError, PTilesError, OSError)):
                BuildingsReader.open(path)
        finally:
            os.unlink(path)

    def test_buildings_random_256_bytes(self):
        """Open a 256-byte file with random data — may succeed parsing but
        fail on seek, or parse garbage silently. Either way, no crash."""
        path = self._write_temp(os.urandom(256))
        try:
            try:
                reader = BuildingsReader.open(path)
                # If we get here, it parsed garbage — verify header is nonsense
                assert reader.header["magic"] != b"PTILES\x00"
                reader.close()
            except (ValueError, OSError, struct.error, PTilesError):
                pass  # Expected to fail for bad data
        finally:
            os.unlink(path)

    def test_water_random_256_bytes(self):
        """Open a water file with 256 random bytes."""
        path = self._write_temp(os.urandom(256))
        try:
            try:
                reader = WaterReader.open(path)
                reader.close()
            except (ValueError, OSError, struct.error, PTilesError):
                pass
        finally:
            os.unlink(path)

    def test_business_random_256_bytes(self):
        """Open a business file with 256 random bytes."""
        path = self._write_temp(os.urandom(256))
        try:
            try:
                reader = BusinessReader.open(path)
                reader.close()
            except (ValueError, OSError, struct.error, PTilesError):
                pass
        finally:
            os.unlink(path)

    def test_roads_random_256_bytes(self):
        """Open a roads file with 256 random bytes."""
        path = self._write_temp(os.urandom(256))
        try:
            try:
                reader = RoadsReader.open(path)
                reader.close()
            except (ValueError, OSError, struct.error, PTilesError):
                pass
        finally:
            os.unlink(path)
