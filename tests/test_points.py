"""
Tests for the camera and signals readers against the published national files.

These pin the exact record counts measured when the readers were written. The
point layers have no per-record length prefix, so a field-width or flag-bit
disagreement with the encoder desyncs the rest of the cell rather than raising
— a count that still matches is the evidence that has not happened.

Skipped when the files are absent.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from ptiles.camera import Camera, CameraReader
from ptiles.points import parse_coarse_index
from ptiles.signals import Signal, SignalsReader

TILES = Path(__file__).resolve().parent.parent / "tiles"
US_CAMERA = TILES / "US.camera.ptiles"
US_SIGNALS = TILES / "US.signals.ptiles"

# Measured against the 2026-07 national build.
CAMERA_EXPECTED = {
    "total": 129_251,
    "direction": 93_623,
    "angle": 299,
    "ALPR": 96_569,
    "camera": 26_690,
    "guard": 94,
    "fixed": 101_511,
    "dome": 8_650,
    "panning": 1_378,
}
SIGNALS_EXPECTED = {
    "total": 2_107_809,
    "direction": 2_310,
    "stop": 980_556,
    "crossing_signals": 536_955,
    "traffic_signals": 536_197,
    "give_way": 54_101,
}


@pytest.mark.skipif(not US_CAMERA.exists(), reason=f"missing {US_CAMERA}")
class TestCameraReader:

    @pytest.fixture(scope="class")
    @classmethod
    def counts(cls):
        r = CameraReader.open(US_CAMERA)
        try:
            c = Counter()
            for entry in r._index:
                for cam in r.get_in_cell(entry["h3_cell"]):
                    c["total"] += 1
                    c[cam.device_type] += 1
                    c[cam.camera_type] += 1
                    if cam.direction is not None:
                        c["direction"] += 1
                    if cam.angle is not None:
                        c["angle"] += 1
            yield c
        finally:
            r.close()

    @pytest.mark.parametrize("key", sorted(CAMERA_EXPECTED))
    def test_counts_match(self, counts, key):
        assert counts[key] == CAMERA_EXPECTED[key]

    def test_header(self):
        with CameraReader.open(US_CAMERA) as r:
            assert r.header["magic"].startswith(b"PTILESC")
            assert r.header["feature_count"] == CAMERA_EXPECTED["total"]

    def test_coarse_index_brackets_every_cell(self):
        """A sample that points at the wrong run is worse than no coarse index."""
        with CameraReader.open(US_CAMERA) as r:
            ci = r.coarse_index
            assert ci is not None
            assert ci.entry_count == len(r._index)
            for i in range(0, len(r._index), max(1, len(r._index) // 50)):
                cell = r._index[i]["h3_cell"]
                lo, hi = ci.bracket(cell)
                assert lo <= i < hi, f"cell {cell:#x} at {i} not in [{lo},{hi})"

    def test_wrong_magic_is_rejected(self):
        if not US_SIGNALS.exists():
            pytest.skip("needs the signals file to mismatch against")
        with pytest.raises(ValueError, match="magic"):
            CameraReader.open(US_SIGNALS)

    def test_directionality(self):
        cam = Camera(1, 0.0, 0.0, "camera", "outdoor", "fixed", direction=90)
        assert cam.is_directional
        # Sweeping units have a bearing that does not constrain coverage.
        assert not Camera(1, 0, 0, "camera", "outdoor", "dome",
                          direction=90).is_directional
        assert not Camera(1, 0, 0, "camera", "outdoor", "fixed",
                          direction=None).is_directional


@pytest.mark.skipif(not US_SIGNALS.exists(), reason=f"missing {US_SIGNALS}")
class TestSignalsReader:

    @pytest.fixture(scope="class")
    @classmethod
    def counts(cls):
        r = SignalsReader.open(US_SIGNALS)
        try:
            c = Counter()
            for entry in r._index:
                for s in r.get_in_cell(entry["h3_cell"]):
                    c["total"] += 1
                    c[s.signal_type] += 1
                    if s.direction is not None:
                        c["direction"] += 1
            yield c
        finally:
            r.close()

    @pytest.mark.parametrize("key", sorted(SIGNALS_EXPECTED))
    def test_counts_match(self, counts, key):
        assert counts[key] == SIGNALS_EXPECTED[key]

    def test_bearings_are_effectively_absent(self, counts):
        """Guards the claim in the module docstring, so it cannot rot silently.

        At 0.1% coverage this layer cannot support approach-direction or
        turn-restriction reasoning.
        """
        assert counts["direction"] / counts["total"] < 0.01

    def test_delay_seconds(self):
        assert Signal(1, 0, 0, "traffic_signals").delay_seconds == 20.0
        assert Signal(1, 0, 0, "stop").delay_seconds == 4.0
        assert Signal(1, 0, 0, "unknown").delay_seconds == 0.0


class TestCoarseIndexParsing:

    def test_rejects_non_ptci(self):
        assert parse_coarse_index(b"NOPE" + b"\x00" * 32) is None

    def test_rejects_short(self):
        assert parse_coarse_index(b"PTCI") is None

    def test_empty(self):
        assert parse_coarse_index(b"") is None
