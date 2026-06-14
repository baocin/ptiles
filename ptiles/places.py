"""
Places reader for PTiles format (.places.ptiles).

Decodes place records (cities, towns, hamlets, etc.) from OSM data.
Supports v1 (single-cell blocks) and v2 (merged-block) index formats.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

import h3

from ptiles.codec import (
    decode_varint,
    zigzag_decode,
    decode_string_u16,
    decode_index_v2,
    decode_merged_block_header,
)
from ptiles.reader import BlockFileReader

logger = logging.getLogger("ptiles.places")

PLACE_TYPE_REVERSE = {
    0: "city",
    1: "town",
    2: "village",
    3: "hamlet",
    4: "neighborhood",
    5: "suburb",
    6: "borough",
    7: "quarter",
    8: "isolated_dwelling",
}


@dataclass(frozen=True, slots=True)
class Place:
    osm_id: int
    lat: float
    lon: float
    place_type: str
    population: int
    name: str
    alt_name: str | None = None
    admin_level: int | None = None


def decode_place(data: bytes, offset: int, prev_osm_id: int) -> tuple[dict, int, int]:
    pos = offset
    delta_raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(delta_raw)
    lon_micro = struct.unpack_from("<i", data, pos)[0]
    lat_micro = struct.unpack_from("<i", data, pos + 4)[0]
    pos += 8
    place_type_idx = data[pos]
    pos += 1
    place_type = PLACE_TYPE_REVERSE.get(place_type_idx, f"unknown({place_type_idx})")
    population_raw, consumed = decode_varint(data, pos)
    pos += consumed
    population = population_raw
    name, consumed = decode_string_u16(data, pos)
    pos += consumed
    flags = data[pos]
    pos += 1
    alt_name = None
    admin_level = None
    if flags & 0x01:
        alt_name, consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x02:
        admin_level = data[pos]
        pos += 1
    return (
        {
            "osm_id": osm_id,
            "lat": lat_micro / 100_000,
            "lon": lon_micro / 100_000,
            "place_type": place_type,
            "population": population,
            "name": name,
            "alt_name": alt_name,
            "admin_level": admin_level,
        },
        pos - offset,
        osm_id,
    )


def decode_block(data: bytes) -> list[dict]:
    places = []
    pos = 0
    prev_osm_id = 0
    while pos < len(data):
        try:
            feat, consumed, prev_osm_id = decode_place(data, pos, prev_osm_id)
            places.append(feat)
            pos += consumed
        except (IndexError, struct.error):
            break
    return places


class PlacesReader(BlockFileReader):
    """Reader for .places.ptiles files. Supports v1 and v2 index formats."""

    def __init__(self, f, filepath):
        self._v2_index = False
        self._file = f
        self._filepath = filepath
        # Read header (magic + 256 bytes included)
        f.seek(0)
        from ptiles.codec import read_header

        self._header = read_header(f)
        self._version = self._header["version"]

        # Check if index size suggests v2 (37 bytes/entry) vs v1 (17 bytes/entry)
        bc = self._header.get("block_count", 0)
        idx_len = self._header["index_length"]
        est_v1 = 4 + bc * 17
        if idx_len > est_v1 + bc * 5 and est_v1 > 0:
            self._v2_index = True

        # Read dictionary
        f.seek(self._header["dict_offset"])
        self._dict_data = f.read(self._header["dict_length"])

        # Read index
        f.seek(self._header["index_offset"])
        index_bytes = f.read(idx_len)
        if self._v2_index:
            self._index = decode_index_v2(index_bytes)
        else:
            from ptiles.codec import read_index

            self._index = read_index(index_bytes)

        # Detect relative offsets
        self._relative_offsets = True
        if self._index:
            first_off = self._index[0]["block_offset"]
            self._relative_offsets = first_off < self._header["blocks_offset"]

    def _read_block(self, cell_int: int) -> list[Place]:
        if self._v2_index:
            raw = self._read_merged_block(cell_int)
        else:
            raw = self.read_block_raw(cell_int)
        if raw is None:
            return []
        raw_dicts = decode_block(raw)
        return [
            Place(
                osm_id=d["osm_id"],
                lat=d["lat"],
                lon=d["lon"],
                place_type=d["place_type"],
                population=d["population"],
                name=d["name"],
                alt_name=d["alt_name"],
                admin_level=d["admin_level"],
            )
            for d in raw_dicts
        ]

    def _read_merged_block(self, cell_int: int) -> bytes | None:
        """Read a v2 merged block and extract data for a specific cell."""
        entry = self._lookup_index(cell_int)
        if entry is None:
            return None
        # Read and decompress the merged block
        raw = None
        file_offset = self.resolve_offset(entry["block_offset"])
        self._file.seek(file_offset)
        compressed = self._file.read(entry["block_length"])
        if self._dict_data:
            import zstandard as zstd

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
        # Parse merged block header
        hdr = decode_merged_block_header(raw)
        cell_index = entry.get("cell_index", 0)
        if cell_index < len(hdr["cell_offsets"]):
            cid, rec_off = hdr["cell_offsets"][cell_index]
            data_start = hdr["record_data_offset"]
            if cell_index + 1 < len(hdr["cell_offsets"]):
                next_off = hdr["cell_offsets"][cell_index + 1][1]
                return raw[data_start + rec_off : data_start + next_off]
            else:
                # Last cell in the merged block: read to end
                return raw[data_start + rec_off :]
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

    def get_in_cell(self, cell: int | str) -> list[Place]:
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        return self._read_block(cell_int)

    def get_in_bounds(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        limit: int = 1000,
    ) -> list[Place]:
        try:
            cells = h3.polygon_to_cells(
                [
                    (min_lat, min_lon),
                    (min_lat, max_lon),
                    (max_lat, max_lon),
                    (max_lat, min_lon),
                ],
                res=7,
            )
        except Exception:
            center_lat = (min_lat + max_lat) / 2
            center_lon = (min_lon + max_lon) / 2
            center_cell = h3.latlng_to_cell(center_lat, center_lon, 7)
            cells = h3.grid_disk(center_cell, 2)
        results = []
        for cell in cells:
            cell_int = int(cell, 16) if isinstance(cell, str) else cell
            for p in self._read_block(cell_int):
                if min_lat <= p.lat <= max_lat and min_lon <= p.lon <= max_lon:
                    results.append(p)
                    if len(results) >= limit:
                        return results
        return results

    def close(self) -> None:
        self._file.close()
