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
    decode_boundary,
)

logger = logging.getLogger(__name__)


class BoundaryMixin:
    """The region-boundary accessor, for readers that are not BlockFileReader.

    buildings, water, business, admin and the point layers each have their own
    __init__ and do not inherit from BlockFileReader, so they pick the boundary
    up from here rather than each carrying a copy.
    """

    _boundary_cache = None

    @property
    def boundary(self) -> list[list[tuple[float, float]]]:
        """The region's own boundary rings, or [] when the file carries none.

        Lets a file be identified and de-duplicated without the admin layer: the
        header bbox separates Alaska from Delaware but not two overlapping
        regional extracts. Read on first use, like the index.
        """
        if self._boundary_cache is None:
            off = self._header.get("boundary_offset", 0)
            length = self._header.get("boundary_length", 0)
            if not off or not length:
                self._boundary_cache = []
            else:
                self._file.seek(off)
                self._boundary_cache = decode_boundary(self._file.read(length))
        return self._boundary_cache


class BlockFileReader(BoundaryMixin):
    """Base reader for PTiles block files.

    Subclasses call super().__init__(f, filepath) or use open() classmethod.
    Provides:
      - header property
      - resolve_offset()
      - read_block_raw() -- seek + decompress with dict fallback
    """

    # Class-level so subclasses with their own __init__ (places, parks, rail)
    # inherit it without each having to remember.
    _boundary_cache = None

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


class MergedBlockReader(BlockFileReader):
    """Base for layers whose cells are packed several to a compressed block.

    The v2 index (38-byte entries) points at a merged block plus the position of
    one cell inside it, so a reader has to slice its cell's bytes out before
    decoding records. Subclasses supply `decode_records`; everything above that
    -- index lookup, decompression with or without the dictionary, the merged
    block header, and cell coverage for a bbox -- is the same for all of them.

    PlacesReader predates this and carries its own copy; it works, so it has
    been left alone rather than refactored under a reader people rely on.
    """

    def decode_records(self, raw: bytes) -> list:
        """Decode one cell's record bytes. Implemented by the subclass."""
        raise NotImplementedError

    def _lookup_index(self, cell_int: int) -> dict | None:
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

    def _cell_bytes(self, cell_int: int) -> bytes | None:
        """Record bytes for one cell, sliced out of its (possibly merged) block."""
        entry = self._lookup_index(cell_int)
        if entry is None:
            return None
        self._file.seek(self.resolve_offset(entry["block_offset"]))
        compressed = self._file.read(entry["block_length"])

        import zstandard as zstd

        raw = None
        if self._dict_data:
            try:
                d = zstd.ZstdCompressionDict(self._dict_data)
                raw = zstd.ZstdDecompressor(dict_data=d).decompress(compressed)
            except Exception:
                raw = None
        if raw is None:
            try:
                raw = zstd.ZstdDecompressor().decompress(compressed)
            except Exception:
                return None

        if not self._v2_index:
            return raw

        hdr = decode_merged_block_header(raw)
        cell_index = entry.get("cell_index", 0)
        offsets = hdr["cell_offsets"]
        if cell_index >= len(offsets):
            return None
        start = hdr["record_data_offset"] + offsets[cell_index][1]
        if cell_index + 1 < len(offsets):
            return raw[start : hdr["record_data_offset"] + offsets[cell_index + 1][1]]
        return raw[start:]  # last cell in the block runs to the end

    def get_in_cell(self, cell: int | str) -> list:
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        raw = self._cell_bytes(cell_int)
        return self.decode_records(raw) if raw else []

    def get_in_bounds(self, min_lat, min_lon, max_lat, max_lon, limit: int = 1000) -> list:
        import h3

        try:
            cells = h3.polygon_to_cells(
                h3.LatLngPoly(
                    [
                        (min_lat, min_lon),
                        (min_lat, max_lon),
                        (max_lat, max_lon),
                        (max_lat, min_lon),
                    ]
                ),
                7,
            )
        except Exception:
            # A box smaller than one cell polygon-fills to nothing; fall back to
            # the cells of its corners so a tight bbox still returns something.
            cells = {
                h3.latlng_to_cell(la, lo, 7)
                for la in (min_lat, max_lat)
                for lo in (min_lon, max_lon)
            }
        out = []
        for cell in cells:
            for rec in self.get_in_cell(cell):
                lat = getattr(rec, "lat", None)
                lon = getattr(rec, "lon", None)
                if lat is not None and lon is not None:
                    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
                        continue
                out.append(rec)
                if len(out) >= limit:
                    return out
        return out
