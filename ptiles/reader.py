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

        # Detect v2 index format (38-byte entries) vs v1 (17-byte entries)
        bc = self._header.get("block_count", 0)
        idx_len = self._header["index_length"]
        est_v1 = 4 + bc * 17
        if idx_len > est_v1 + bc * 5 and est_v1 > 0:
            self._v2_index = True

        # The dictionary and the spatial index are read on first use, not here.
        # The index decodes to one dict per block -- ~1 KB of Python object per
        # entry, so a file with 86k blocks costs ~98 MB just to open. A client
        # holding many countries opens far more files than it queries, and the
        # bounds test that decides which to query needs only the header.
        self._index_cache = None
        self._dict_cache = None
        self._relative_offsets_cache = None

    @classmethod
    def open(cls, path: str | os.PathLike) -> "BlockFileReader":
        """Open a .ptiles file. Subclasses may override return type."""
        f = open(path, "rb")
        return cls(f, str(path))

    @property
    def header(self) -> dict:
        return self._header

    @property
    def _index(self) -> list:
        """Spatial index, decoded on first use. See __init__ for why."""
        if self._index_cache is None:
            self._file.seek(self._header["index_offset"])
            raw = self._file.read(self._header["index_length"])
            self._index_cache = (
                decode_index_v2(raw) if self._v2_index else read_index(raw)
            )
        return self._index_cache

    @property
    def _dict_data(self) -> bytes:
        """Zstd dictionary, read on first use."""
        if self._dict_cache is None:
            self._file.seek(self._header["dict_offset"])
            self._dict_cache = self._file.read(self._header["dict_length"])
        return self._dict_cache

    @property
    def _relative_offsets(self) -> bool:
        if self._relative_offsets_cache is None:
            idx = self._index
            self._relative_offsets_cache = (
                idx[0]["block_offset"] < self._header["blocks_offset"] if idx else True
            )
        return self._relative_offsets_cache

    @property
    def index_loaded(self) -> bool:
        """Whether the index has actually been decoded. For tests and metrics."""
        return self._index_cache is not None

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
