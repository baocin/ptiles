"""
Parsing and boundary-condition tests for ptiles.codec.

These exist because of what a fault probe turned up: the length-prefixed
string decoders did not fail on a corrupt length, they *succeeded wrongly*.
Python slices clamp, so `decode_string_u8(b"\\xff\\x41", 0)` returned
`('A', 256)` — a plausible short string plus a consumed count 128x past the
end of the buffer. Records are walked sequentially by that count, so one bad
byte silently turned the rest of a block into garbage that still decoded into
well-formed objects. That is worse than a crash, and it is what these guard.
"""

from __future__ import annotations

import io
import struct

import pytest

from ptiles import codec


class TestVarint:
    def test_single_byte(self):
        assert codec.decode_varint(b"\x7f", 0) == (127, 1)

    def test_multi_byte(self):
        assert codec.decode_varint(b"\xac\x02", 0) == (300, 2)

    def test_zero(self):
        assert codec.decode_varint(b"\x00", 0) == (0, 1)

    def test_max_64_bit(self):
        value, consumed = codec.decode_varint(b"\xff" * 9 + b"\x01", 0)
        assert value == 2**64 - 1
        assert consumed == codec.MAX_VARINT_BYTES

    def test_decodes_at_an_offset(self):
        assert codec.decode_varint(b"\x00\x00\x7f", 2) == (127, 1)

    def test_truncated_raises(self):
        # Continuation bit set on the last byte available.
        with pytest.raises(ValueError, match="truncated varint"):
            codec.decode_varint(b"\x80", 0)

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="truncated varint"):
            codec.decode_varint(b"", 0)

    def test_over_long_raises_instead_of_spinning(self):
        # All-continuation input used to walk to the end of the buffer
        # accumulating a nonsense integer.
        with pytest.raises(ValueError, match="over-long varint"):
            codec.decode_varint(b"\xff" * 32, 0)

    def test_position_past_end_raises(self):
        with pytest.raises(ValueError, match="truncated varint"):
            codec.decode_varint(b"\x01", 5)

    def test_zigzag_round_trip(self):
        for n in (0, 1, -1, 127, -128, 2**31 - 1, -(2**31)):
            assert codec.zigzag_decode(codec.zigzag_encode(n)) == n


class TestLengthPrefixedStrings:
    def test_u8_exact_fit(self):
        assert codec.decode_string_u8(b"\x02AB", 0) == ("AB", 3)

    def test_u8_zero_length(self):
        assert codec.decode_string_u8(b"\x00", 0) == ("", 1)

    def test_u16_exact_fit(self):
        assert codec.decode_string_u16(b"\x02\x00AB", 0) == ("AB", 4)

    def test_u16_zero_length(self):
        assert codec.decode_string_u16(b"\x00\x00", 0) == ("", 2)

    def test_u8_length_past_buffer_raises(self):
        # Used to return ('A', 256) — a short string and a consumed count that
        # desyncs every following record.
        with pytest.raises(ValueError, match="declares 255 bytes"):
            codec.decode_string_u8(b"\xffA", 0)

    def test_u16_length_past_buffer_raises(self):
        with pytest.raises(ValueError, match="declares 65535 bytes"):
            codec.decode_string_u16(b"\xff\xffA", 0)

    def test_u8_one_byte_short_raises(self):
        # The off-by-one case, which a length check on the prefix alone misses.
        with pytest.raises(ValueError):
            codec.decode_string_u8(b"\x03AB", 0)

    def test_u8_missing_length_byte_raises(self):
        with pytest.raises(ValueError, match="truncated u8 string length"):
            codec.decode_string_u8(b"", 0)

    def test_u16_truncated_length_field_raises(self):
        with pytest.raises(ValueError, match="truncated u16 string length"):
            codec.decode_string_u16(b"\x02", 0)

    def test_invalid_utf8_is_replaced_not_raised(self):
        # OSM carries occasional mis-encoded names; losing a character beats
        # losing the block. Consumed count must still be exact.
        s, consumed = codec.decode_string_u8(b"\x02\xff\xfe", 0)
        assert consumed == 3
        assert len(s) == 2

    def test_multibyte_utf8_counts_bytes_not_characters(self):
        payload = "café".encode("utf-8")          # 5 bytes, 4 characters
        s, consumed = codec.decode_string_u8(bytes([len(payload)]) + payload, 0)
        assert s == "café"
        assert consumed == 1 + len(payload)


class TestIndexStride:
    def _section(self, count: int, stride: int) -> bytes:
        return struct.pack("<I", count) + b"\x00" * (count * stride)

    def test_v1_detected(self):
        assert codec.index_entry_stride(self._section(10, 19)) == codec.INDEX_ENTRY_SIZE

    def test_v2_detected(self):
        assert codec.index_entry_stride(self._section(10, 38)) == codec.INDEX_ENTRY_SIZE_V2

    def test_empty_index_is_v1_by_convention(self):
        assert codec.index_entry_stride(struct.pack("<I", 0)) == codec.INDEX_ENTRY_SIZE

    def test_unknown_stride_raises(self):
        with pytest.raises(ValueError, match="cannot determine index stride"):
            codec.index_entry_stride(self._section(10, 12))

    def test_error_reports_the_implied_width(self):
        with pytest.raises(ValueError, match="12 bytes each"):
            codec.index_entry_stride(self._section(10, 12))

    def test_too_short_to_hold_a_count_raises(self):
        with pytest.raises(ValueError, match="too short"):
            codec.index_entry_stride(b"")

    def test_count_with_no_entries_raises(self):
        with pytest.raises(ValueError):
            codec.index_entry_stride(struct.pack("<I", 5))

    def test_read_index_auto_returns_stride(self):
        entries, stride = codec.read_index_auto(self._section(0, 19))
        assert entries == []
        assert stride == codec.INDEX_ENTRY_SIZE


class TestHeader:
    def test_too_small_raises(self):
        with pytest.raises(ValueError, match="too small"):
            codec.read_header(io.BytesIO(b""))

    def test_partial_header_raises(self):
        with pytest.raises(ValueError, match="too small"):
            codec.read_header(io.BytesIO(b"PTILESF\x00\x09" + b"\x00" * 50))

    def test_all_zero_header_parses(self):
        # Structurally valid, semantically empty. It must not raise — callers
        # decide whether a zero magic is acceptable.
        h = codec.read_header(io.BytesIO(b"\x00" * 256))
        assert h["version"] == 0
        assert h["feature_count"] == 0


class TestModuleSurface:
    def test_every_exported_name_exists(self):
        """`__all__` listed decode_water_record, which lives in ptiles.water.

        The only symptom was `from ptiles.codec import *` raising
        AttributeError — invisible to every test, because nothing star-imports
        in normal use.
        """
        missing = [n for n in codec.__all__ if not hasattr(codec, n)]
        assert missing == [], f"__all__ names not defined in ptiles.codec: {missing}"

    def test_star_import_works(self):
        ns: dict = {}
        exec("from ptiles.codec import *", ns)
        assert "decode_varint" in ns


class TestDecompression:
    def test_non_zstd_input_returns_none(self):
        assert codec.decompress_block(b"definitely not zstd", b"") is None

    def test_empty_input_returns_none(self):
        assert codec.decompress_block(b"", b"") is None

    def test_truncated_frame_returns_none(self):
        assert codec.decompress_block(b"\x28\xb5\x2f\xfd\x00", b"") is None
