"""
Shared plumbing for the point layers (cameras, signals).

Both are US-wide files built by scripts/build_points.py, and both differ from
the per-state layers in three ways a reader has to get right:

* 38-byte index entries, where ``codec.read_index``'s 19-byte assumption
  produces nonsense offsets rather than an error;
* merged blocks — one zstd frame holds up to CELLS_PER_BLOCK cells, with a
  cell table at the front pointing into a shared record region;
* a ``PTCI`` coarse index in the aux region, which lets a cell be located from
  a few KiB instead of pulling the whole index (4 MiB for signals).

Records within a cell run back to back with no length prefix, so a decoder
that mis-sizes one field corrupts the rest of the cell. Each reader therefore
cross-checks the number of records it decoded against the index's
``feature_count`` and complains rather than returning a short list quietly.
"""

from __future__ import annotations

import io
import logging
import os
import struct
from dataclasses import dataclass
from typing import Any, Callable

import zstandard as zstd

from ptiles.codec import (
    INDEX_ENTRY_SIZE_V2,
    binary_search_index,
    decompress_block,
    read_header,
    read_index_auto,
)

logger = logging.getLogger("ptiles.points")

COARSE_MAGIC = b"PTCI"


@dataclass(frozen=True, slots=True)
class CoarseIndex:
    """Sampled index from the aux region — see SPEC.md 'Coarse index (aux)'.

    Not used when the whole file is already open locally; it exists so an HTTP
    reader can binary-search ~5 KiB of samples and then range-request only the
    slice of the real index that brackets its cell.
    """

    stride: int
    entry_count: int
    samples: tuple[tuple[int, int], ...]  # (h3_cell, entry_index), cell-sorted

    def bracket(self, cell: int) -> tuple[int, int]:
        """Index-entry range [lo, hi) that must contain `cell` if present."""
        lo, hi = 0, len(self.samples) - 1
        found = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.samples[mid][0] <= cell:
                found = mid
                lo = mid + 1
            else:
                hi = mid - 1
        start = self.samples[found][1]
        end = (self.samples[found + 1][1]
               if found + 1 < len(self.samples) else self.entry_count)
        return start, end


def parse_coarse_index(aux: bytes) -> CoarseIndex | None:
    """Parse a PTCI aux region, or None if it isn't one."""
    if len(aux) < 20 or aux[:4] != COARSE_MAGIC:
        return None
    version = aux[4]
    if version != 1:
        logger.warning("coarse index version %d not understood; ignoring", version)
        return None
    stride, sample_count, entry_count = struct.unpack_from("<III", aux, 8)
    samples = tuple(
        struct.unpack_from("<QI", aux, 20 + i * 12) for i in range(sample_count)
    )
    return CoarseIndex(stride=stride, entry_count=entry_count, samples=samples)


class PointLayerReader:
    """Base for US-wide point layers stored in merged blocks."""

    magic: bytes = b""
    # Subclasses supply a decoder taking (data, pos, prev_osm_id) and
    # returning (feature, bytes_consumed, osm_id).
    decode_record: Callable[..., tuple[Any, int, int]]

    def __init__(self, f: io.BufferedReader, filepath: str):
        self._file = f
        self._filepath = filepath
        self._header = read_header(f)
        if self.magic and not self._header["magic"].startswith(self.magic):
            raise ValueError(
                f"{filepath}: expected magic {self.magic!r}, "
                f"got {self._header['magic']!r}"
            )

        aux_len = self._header.get("aux_length", 0)
        self._coarse = None
        if aux_len:
            f.seek(self._header["aux_offset"])
            self._coarse = parse_coarse_index(f.read(aux_len))

        # Dictionary and index are read on first use, not here. See
        # ptiles/reader.py: the index is ~1 KB of Python object per block, and a
        # multi-country client opens far more files than it queries. The stride
        # warning below moves with it, so it is emitted on first query rather
        # than at open.
        self._index_cache = None
        self._dict_cache = None
        self._raw_block_cache: dict[int, bytes] = {}
        self._raw_block_cache_max = 64

    @property
    def _index(self):
        """Spatial index, decoded on first use -- see __init__."""
        if self._index_cache is None:
            self._file.seek(self._header["index_offset"])
            raw = self._file.read(self._header["index_length"])
            self._index_cache, stride = read_index_auto(raw)
            if stride != INDEX_ENTRY_SIZE_V2:
                logger.warning(
                    "%s: index stride %d, expected %d — this layer should use "
                    "wide entries", self._filepath, stride, INDEX_ENTRY_SIZE_V2,
                )
        return self._index_cache

    @property
    def _dict_data(self):
        if self._dict_cache is None:
            self._file.seek(self._header["dict_offset"])
            self._dict_cache = self._file.read(self._header["dict_length"])
        return self._dict_cache

    @property
    def index_loaded(self):
        return self._index_cache is not None

    @classmethod
    def open(cls, path: str | os.PathLike):
        return cls(open(path, "rb"), str(path))

    @property
    def header(self) -> dict:
        return self._header

    @property
    def coarse_index(self) -> CoarseIndex | None:
        return self._coarse

    def close(self) -> None:
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- block access ---

    def _read_raw_block(self, file_offset: int, block_length: int) -> bytes | None:
        cached = self._raw_block_cache.get(file_offset)
        if cached is not None:
            return cached
        self._file.seek(file_offset)
        compressed = self._file.read(block_length)
        raw = None
        if self._dict_data:
            try:
                raw = decompress_block(compressed, self._dict_data)
            except Exception:
                pass
        if raw is None:
            try:
                raw = zstd.ZstdDecompressor().decompress(compressed)
            except Exception as e:
                logger.warning("block at %d failed to decompress: %s",
                               file_offset, e)
                return None
        if len(self._raw_block_cache) >= self._raw_block_cache_max:
            self._raw_block_cache.clear()
        self._raw_block_cache[file_offset] = raw
        return raw

    def _cell_body(self, raw: bytes, cell_int: int) -> bytes | None:
        """Slice one cell's records out of a merged block."""
        _, _, cell_count = struct.unpack_from("<iiI", raw, 0)
        table = [struct.unpack_from("<QI", raw, 12 + 12 * i)
                 for i in range(cell_count)]
        body_start = 12 + 12 * cell_count
        for i, (cid, off) in enumerate(table):
            if cid != cell_int:
                continue
            start = body_start + off
            end = (body_start + table[i + 1][1] if i + 1 < cell_count
                   else len(raw))
            return raw[start:end]
        return None

    def get_in_cell(self, cell: int | str) -> list[Any]:
        """All features in one H3 res-7 cell."""
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        entry = binary_search_index(self._index, cell_int)
        if entry is None:
            return []
        raw = self._read_raw_block(entry["block_offset"], entry["block_length"])
        if raw is None:
            return []
        body = self._cell_body(raw, cell_int)
        if body is None:
            logger.warning("cell %#x absent from the block its index entry "
                           "points at", cell_int)
            return []

        features: list[Any] = []
        pos = 0
        prev_id = 0
        while pos < len(body):
            try:
                feature, consumed, prev_id = self.decode_record(body, pos, prev_id)
            except Exception as e:
                logger.warning("cell %#x: record %d desynced at byte %d: %s",
                               cell_int, len(features), pos, e)
                break
            features.append(feature)
            pos += consumed

        expected = entry["feature_count"]
        if len(features) != expected:
            # Without length prefixes there is no resync point, so a mismatch
            # means everything after the bad record in this cell was lost.
            logger.warning("cell %#x: decoded %d records, index says %d",
                           cell_int, len(features), expected)
        return features

    def get_in_bounds(self, min_lat: float, min_lon: float,
                      max_lat: float, max_lon: float,
                      limit: int = 10_000) -> list[Any]:
        """All features whose cell bbox overlaps the given box.

        Uses the per-cell bounding boxes in the wide index entries to skip
        blocks without decompressing them.
        """
        lo_lon = round(min_lon * 100_000)
        hi_lon = round(max_lon * 100_000)
        lo_lat = round(min_lat * 100_000)
        hi_lat = round(max_lat * 100_000)

        out: list[Any] = []
        for entry in self._index:
            if "min_lon" not in entry:
                continue
            if (entry["max_lon"] < lo_lon or entry["min_lon"] > hi_lon
                    or entry["max_lat"] < lo_lat or entry["min_lat"] > hi_lat):
                continue
            for feature in self.get_in_cell(entry["h3_cell"]):
                if (min_lat <= feature.lat <= max_lat
                        and min_lon <= feature.lon <= max_lon):
                    out.append(feature)
                    if len(out) >= limit:
                        return out
        return out
