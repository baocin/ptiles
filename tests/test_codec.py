"""Tests for ptiles.codec — varint, zigzag, coordinate, and string en/decoding."""

from __future__ import annotations

import struct

import pytest

from ptiles.codec import (
    encode_varint,
    decode_varint,
    zigzag_encode,
    zigzag_decode,
    coord_to_micro,
    micro_to_coord,
    encode_coordinates,
    decode_coordinates,
    encode_string_u16,
    decode_string_u16,
    encode_string_u8,
    decode_string_u8,
)


class TestVarint:
    """Varint round-trip tests."""

    @pytest.mark.parametrize("value", [0, 1, 127, 128, 65535, 2**32 - 1, 2**64 - 1])
    def test_varint_roundtrip(self, value: int):
        """Encode and decode varint, verify value matches."""
        encoded = encode_varint(value)
        decoded, consumed = decode_varint(encoded, 0)
        assert decoded == value
        assert consumed == len(encoded)

    def test_varint_decode_partial(self):
        """Decode from a larger buffer should consume only the varint bytes."""
        data = encode_varint(42) + b"\x00\xff"
        decoded, consumed = decode_varint(data, 0)
        assert decoded == 42
        assert consumed < len(data)

    def test_varint_decode_at_offset(self):
        """Decode varint starting at a non-zero offset."""
        data = b"\x00" * 5 + encode_varint(999)
        decoded, consumed = decode_varint(data, 5)
        assert decoded == 999

    def test_varint_large_values(self):
        """Edge cases for very large varints."""
        for v in [2**31, 2**63, (2**64) - 1]:
            encoded = encode_varint(v)
            decoded, consumed = decode_varint(encoded, 0)
            assert decoded == v
            assert consumed == len(encoded)


class TestZigzag:
    """Zigzag encode/decode tests."""

    @pytest.mark.parametrize("value", [
        0, -1, 1, -1000000, 1000000,
        -2**31,  # i32::MIN
        2**31 - 1,  # i32::MAX
    ])
    def test_zigzag_roundtrip(self, value: int):
        """Encode and decode zigzag, verify value matches."""
        encoded = zigzag_encode(value)
        decoded = zigzag_decode(encoded)
        assert decoded == value

    def test_zigzag_through_varint(self):
        """Zigzag value round-trips through varint encoding."""
        for v in [0, -1, 1, -42, 42, -1000000, 1000000]:
            encoded = encode_varint(zigzag_encode(v))
            zig_raw, _ = decode_varint(encoded, 0)
            decoded = zigzag_decode(zig_raw)
            assert decoded == v

    def test_zigzag_signed_int32_boundaries(self):
        """Test zigzag at int32 boundaries."""
        for v in [-2**31, -(2**31 - 1), 2**31 - 1, 2**31 - 2]:
            assert zigzag_decode(zigzag_encode(v)) == v

    def test_zigzag_signed_int64(self):
        """Test zigzag with int64 values."""
        for v in [-(2**63), 2**63 - 1, -(2**62), 2**62]:
            assert zigzag_decode(zigzag_encode(v)) == v


class TestCoordinateEncodeDecode:
    """Coordinate encoding round-trip tests."""

    def _roundtrip(self, coords: list[tuple[float, float]]):
        """Helper: encode then decode coords, verify within tolerance."""
        encoded, first_lon, first_lat = encode_coordinates(coords)
        decoded, consumed = decode_coordinates(
            encoded, 0, first_lon, first_lat, len(coords)
        )
        assert len(decoded) == len(coords)
        assert consumed == len(encoded)
        for (orig_lon, orig_lat), (dec_lon, dec_lat) in zip(coords, decoded):
            assert abs(orig_lon - dec_lon) < 1e-5, f"lon mismatch: {orig_lon} != {dec_lon}"
            assert abs(orig_lat - dec_lat) < 1e-5, f"lat mismatch: {orig_lat} != {dec_lat}"

    def test_single_coordinate(self):
        """A single coordinate (no deltas)."""
        self._roundtrip([(-86.7816, 36.1627)])

    def test_two_coordinates(self):
        """Two coordinate pairs."""
        self._roundtrip([
            (-86.7816, 36.1627),
            (-86.7820, 36.1630),
        ])

    def test_building_polygon(self):
        """A small building footprint (closed polygon)."""
        self._roundtrip([
            (-86.7816, 36.1627),
            (-86.7815, 36.1628),
            (-86.7814, 36.1627),
            (-86.7815, 36.1626),
            (-86.7816, 36.1627),  # close ring
        ])

    def test_road_linestring(self):
        """A short road segment (linestring)."""
        self._roundtrip([
            (-86.7900, 36.1600),
            (-86.7895, 36.1605),
            (-86.7890, 36.1610),
            (-86.7885, 36.1615),
        ])

    def test_large_deltas(self):
        """Coordinates with large separation between points."""
        self._roundtrip([
            (-90.0, 35.0),
            (-85.0, 40.0),
        ])

    def test_negative_coordinates(self):
        """Coordinates crossing the equator and prime meridian."""
        self._roundtrip([
            (0.0, 0.0),
            (0.001, -0.001),
            (-0.001, 0.001),
        ])

    def test_microdegree_precision(self):
        """Verify microdegree precision is preserved."""
        coords = [
            (-86.78164, 36.16273),
            (-86.78155, 36.16282),
            (-86.78146, 36.16273),
        ]
        self._roundtrip(coords)


class TestStringEncoding:
    """String encoding round-trip tests."""

    def test_string_u16_roundtrip(self):
        """Encode and decode a string with u16 length prefix."""
        strings = [
            "",
            "hello",
            "Nashville",
            "Cumberland River",
            "A" * 300,  # exceeds u8 limit, fine for u16
            "123 Main Street, Nashville, TN 37201",
            "Caf\u00e9 Fran\u00e7ais",  # unicode
            "\n\t\r",  # whitespace/control chars
        ]
        for s in strings:
            encoded = encode_string_u16(s)
            decoded, consumed = decode_string_u16(encoded, 0)
            assert decoded == s
            assert consumed == len(encoded)

    def test_string_u16_at_offset(self):
        """Decode a u16-prefixed string from a buffer with prefix bytes."""
        data = b"\x00" * 3 + encode_string_u16("offset test")
        decoded, consumed = decode_string_u16(data, 3)
        assert decoded == "offset test"

    def test_string_u8_roundtrip(self):
        """Encode and decode a string with u8 length prefix."""
        strings = [
            "",
            "hello",
            "Nashville",
            "Cumberland River",
            "A" * 255,  # max for u8
            "Caf\u00e9",
        ]
        for s in strings:
            encoded = encode_string_u8(s)
            decoded, consumed = decode_string_u8(encoded, 0)
            assert decoded == s[:255]  # u8 truncates at 255
            assert consumed == len(encoded)

    def test_string_u8_truncation(self):
        """Strings longer than 255 bytes are truncated by encode_string_u8."""
        long_str = "A" * 500
        encoded = encode_string_u8(long_str)
        decoded, consumed = decode_string_u8(encoded, 0)
        assert len(decoded) == 255
        assert decoded == "A" * 255

    def test_string_u8_at_offset(self):
        """Decode a u8-prefixed string from a buffer with prefix bytes."""
        data = b"\xff" * 2 + encode_string_u8("offset")
        decoded, consumed = decode_string_u8(data, 2)
        assert decoded == "offset"

    def test_empty_string_roundtrip(self):
        """Empty strings encode/decode correctly for both u8 and u16."""
        for enc_fn, dec_fn in [(encode_string_u16, decode_string_u16),
                               (encode_string_u8, decode_string_u8)]:
            encoded = enc_fn("")
            decoded, consumed = dec_fn(encoded, 0)
            assert decoded == ""
            assert consumed == len(encoded)
