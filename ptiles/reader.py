"""
BlockFileReader — base class for all PTiles layer readers.

Handles the common IO pattern: open file, read header + dict + index,
detect relative offsets, resolve block offsets, decompress with dict
fallback. Each layer reader (BuildingsReader, RoadsReader, etc.)
inherits from this and adds only record-parsing logic.
"""

from __future__ import annotations

import io
import logging
import os
import zstandard as zstd

from ptiles.codec import (
    read_header,
    read_index,
    binary_search_index,
    decode_index_v2,
    decode_merged_block_header,
)

logger = logging.getLogger(__name__)


class BlockFileReader:
    """Base reader for PTiles block files.

    Subclasses call super().__init__(f, filepath) or use open() classmethod.
    Provides:
      - header property
      - resolve_offset()
      - read_block_raw() -- seek + decompress with dict fallback
    """

    def __init__(self, f: io.BufferedReader, filepath: str):
        self._file = f
        self._filepath = filepath
        self._header = read_header(f)
        self._version = self._header["version"]
        self._v2_index = False

        # Read zstd dictionary
        f.seek(self._header["dict_offset"])
        self._dict_data = f.read(self._header["dict_length"])

        # Detect v2 index format (38-byte entries) vs v1 (17-byte entries)
        bc = self._header.get("block_count", 0)
        idx_len = self._header["index_length"]
        est_v1 = 4 + bc * 17
        if idx_len > est_v1 + bc * 5 and est_v1 > 0:
            self._v2_index = True

        # Read spatial index
        f.seek(self._header["index_offset"])
        index_bytes = f.read(idx_len)
        if self._v2_index:
            self._index = decode_index_v2(index_bytes)
        else:
            self._index = read_index(index_bytes)

        # Detect relative offsets
        self._relative_offsets = True
        if self._index:
            first_off = self._index[0]["block_offset"]
            self._relative_offsets = first_off < self._header["blocks_offset"]

    @classmethod
    def open(cls, path: str | os.PathLike) -> "BlockFileReader":
        """Open a .ptiles file. Subclasses may override return type."""
        f = open(path, "rb")
        return cls(f, str(path))

    @property
    def header(self) -> dict:
        return self._header

    def resolve_offset(self, offset: int) -> int:
        """Convert relative index offset to absolute file offset."""
        if self._relative_offsets:
            return self._header["blocks_offset"] + offset
        return offset

    def lookup_cell(self, cell_int: int) -> dict | None:
        """Binary-search the index for a cell. Returns entry or None."""
        return binary_search_index(self._index, cell_int)

    def read_block_raw(self, cell_int: int) -> bytes | None:
        """Read and decompress a block for a given H3 cell.

        Returns decompressed bytes, or None if cell not in index.
        """
        entry = binary_search_index(self._index, cell_int)
        if entry is None:
            return None

        file_offset = self.resolve_offset(entry["block_offset"])
        self._file.seek(file_offset)
        compressed = self._file.read(entry["block_length"])

        raw = None
        if self._dict_data:
            try:
                d = zstd.ZstdCompressionDict(self._dict_data)
                dctx = zstd.ZstdDecompressor(dict_data=d)
                raw = dctx.decompress(compressed)
            except Exception:
                pass
        if raw is None:
            try:
                raw = zstd.ZstdDecompressor().decompress(compressed)
            except Exception as e:
                logger.warning("Decompress failed for cell %d: %s", cell_int, e)
                return None
        return raw

    def _read_merged_block(self, cell_int: int) -> bytes | None:
        """Read a v2 merged block and extract data for a specific cell."""
        entry = self._lookup_index(cell_int)
        if entry is None:
            return None
        file_offset = self.resolve_offset(entry["block_offset"])
        self._file.seek(file_offset)
        compressed = self._file.read(entry["block_length"])
        raw = None
        if self._dict_data:
            try:
                d = zstd.ZstdCompressionDict(self._dict_data)
                raw = zstd.ZstdDecompressor(dict_data=d).decompress(compressed)
            except Exception:
                pass
        if raw is None:
            try:
                raw = zstd.ZstdDecompressor().decompress(compressed)
            except Exception:
                return None
        hdr = decode_merged_block_header(raw)
        ci = entry.get("cell_index", 0)
        if ci < len(hdr["cell_offsets"]):
            _, rec_off = hdr["cell_offsets"][ci]
            ds = hdr["record_data_offset"]
            if ci + 1 < len(hdr["cell_offsets"]):
                return raw[ds + rec_off : ds + hdr["cell_offsets"][ci + 1][1]]
            return raw[ds + rec_off :]
        return None

    def _lookup_index(self, cell_int: int) -> dict | None:
        """Binary search the index for a cell."""
        entries = self._index
        lo, hi = 0, len(entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if entries[mid]["h3_cell"] < cell_int:
                lo = mid + 1
            else:
                hi = mid
        if lo < len(entries) and entries[lo]["h3_cell"] == cell_int:
            return entries[lo]
        return None

    def close(self) -> None:
        """Close the underlying file."""
        try:
            self._file.close()
        except Exception:
            pass
