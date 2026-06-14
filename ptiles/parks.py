"""
Parks reader for PTiles format (.parks.ptiles).

Decodes park/ protected area polygon features. Provides ParkFeature
dataclass and ParkReader with get_in_cell, get_in_bounds.
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
)
from ptiles.reader import BlockFileReader

logger = logging.getLogger("ptiles.parks")


@dataclass(frozen=True, slots=True)
class ParkFeature:
    osm_id: int
    park_type: str
    coords: tuple[tuple[float, float], ...]
    name: str | None = None


def decode_park(data: bytes, offset: int, prev_osm_id: int) -> tuple[dict, int, int]:
    pos = offset
    delta_raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(delta_raw)
    vertex_count = data[pos]
    pos += 1
    if vertex_count == 255:
        vertex_count = struct.unpack_from("<H", data, pos)[0]
        pos += 2
    first_lon = struct.unpack_from("<i", data, pos)[0]
    first_lat = struct.unpack_from("<i", data, pos + 4)[0]
    pos += 8
    from ptiles.codec import decode_coordinates

    coords_list, consumed = decode_coordinates(
        data, pos, first_lon, first_lat, vertex_count
    )
    coords = coords_list
    pos += consumed
    park_type_len = data[pos]
    pos += 1
    park_type = data[pos : pos + park_type_len].decode("utf-8", errors="replace")
    pos += park_type_len
    flags = data[pos]
    pos += 1
    name = None
    if flags & 0x01:
        name, consumed = decode_string_u16(data, pos)
        pos += consumed
    return (
        {
            "osm_id": osm_id,
            "park_type": park_type,
            "coords": coords,
            "name": name,
        },
        pos - offset,
        osm_id,
    )


def decode_block(data: bytes) -> list[dict]:
    features = []
    pos = 0
    prev_osm_id = 0
    while pos < len(data):
        try:
            feat, consumed, prev_osm_id = decode_park(data, pos, prev_osm_id)
            features.append(feat)
            pos += consumed
        except (IndexError, struct.error):
            break
    return features


class ParkReader(BlockFileReader):
    """Reader for .parks.ptiles files."""

    def _read_block(self, cell_int: int) -> list[ParkFeature]:
        raw = None
        if self._v2_index:
            raw = self._read_merged_block(cell_int)
        else:
            raw = self.read_block_raw(cell_int)
        if raw is None:
            return []
        raw_dicts = decode_block(raw)
        return [
            ParkFeature(
                osm_id=d["osm_id"],
                park_type=d["park_type"],
                coords=tuple(d["coords"]),
                name=d["name"],
            )
            for d in raw_dicts
        ]

    def get_in_cell(self, cell: int | str) -> list[ParkFeature]:
        cell_int = int(cell, 16) if isinstance(cell, str) else cell
        return self._read_block(cell_int)

    def get_in_bounds(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        limit: int = 1000,
    ) -> list[ParkFeature]:
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
            clat = (min_lat + max_lat) / 2
            clon = (min_lon + max_lon) / 2
            center_cell = h3.latlng_to_cell(clat, clon, 7)
            cells = h3.grid_disk(center_cell, 2)
        results = []
        for cell in cells:
            cell_int = int(cell, 16) if isinstance(cell, str) else cell
            for p in self._read_block(cell_int):
                if (
                    min_lat <= p.coords[0][1] <= max_lat
                    and min_lon <= p.coords[0][0] <= max_lon
                ):
                    results.append(p)
                    if len(results) >= limit:
                        return results
        return results

    def close(self) -> None:
        self._file.close()
