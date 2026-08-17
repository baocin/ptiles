"""Reader for .trails_v1.ptiles files (PTILESH).

Trails are paths, tracks, cycleways and steps, plus trailheads as points, built
by scripts/build_trails.py. The layer has been published since the 2026-08-07
snapshot with no reader on this side, so nothing could open it.

Record format, per cell, concatenated:

    varint zigzag osm_id delta (from the previous record in the cell)
    u8   geom_type          0 = linestring, 1 = point
    if linestring:
        u16  vertex_count
        i32  first_lon, first_lat   micro-degrees (1e5)
        varint zigzag lon/lat deltas x (vertex_count - 1)
    if point:
        i32  lon, lat
    u8   trail_type index
    u8   surface index
    u8   sac index          SAC hiking scale, 0 = unset
    u8   flags              0x01 = name follows
    if flags & 0x01:
        u16 name_len + utf-8 name
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ptiles.codec import decode_varint, zigzag_decode
from ptiles.reader import MergedBlockReader

# Index order must match scripts/build_trails.py. TRAIL_TYPES then NODE_TYPES.
TRAIL_TYPES = [
    "path", "track", "bridleway", "cycleway", "footway", "steps", "trailhead",
]
SURFACES = [
    "", "paved", "asphalt", "concrete", "gravel", "compacted", "fine_gravel",
    "dirt", "ground", "grass", "sand", "wood", "boardwalk",
]
SAC = [
    "", "hiking", "mountain_hiking", "demanding_mountain_hiking",
    "alpine_hiking", "demanding_alpine_hiking", "difficult_alpine_hiking",
]

MICRO = 100_000


def _lookup(table: list[str], idx: int) -> str:
    return table[idx] if 0 <= idx < len(table) else ""


@dataclass
class Trail:
    osm_id: int
    trail_type: str
    lat: float
    """First vertex, so a trail sorts and filters like a point feature."""
    lon: float
    is_point: bool = False
    """True for a trailhead node, False for a way."""
    surface: str = ""
    sac: str = ""
    """SAC hiking scale. Empty when the way carries no difficulty tag."""
    name: str | None = None
    # v2 additions. name:en is the valuable one outside the US.
    name_en: str | None = None
    brand: str | None = None
    park_osm_id: int | None = None
    """The park this trail *starts* in, when it starts in one.

    A trail can enter and leave a park, so this is a claim about the first
    vertex, not about the whole way. Computed at build time against the parks
    layer; None means the first vertex fell outside every park polygon, or that
    parks were not built when this file was.
    """
    coords: list[tuple[float, float]] = field(default_factory=list)
    """(lon, lat) pairs. A single pair for a point."""


def decode_trail(data: bytes, pos: int, prev_osm_id: int) -> tuple[Trail, int, int]:
    raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(raw)

    geom_type = data[pos]
    pos += 1

    coords: list[tuple[float, float]] = []
    if geom_type == 0:
        (count,) = struct.unpack_from("<H", data, pos)
        pos += 2
        lon, lat = struct.unpack_from("<ii", data, pos)
        pos += 8
        coords.append((lon / MICRO, lat / MICRO))
        for _ in range(count - 1):
            dlon, consumed = decode_varint(data, pos)
            pos += consumed
            dlat, consumed = decode_varint(data, pos)
            pos += consumed
            lon += zigzag_decode(dlon)
            lat += zigzag_decode(dlat)
            coords.append((lon / MICRO, lat / MICRO))
    else:
        lon, lat = struct.unpack_from("<ii", data, pos)
        pos += 8
        coords.append((lon / MICRO, lat / MICRO))

    trail_type = _lookup(TRAIL_TYPES, data[pos]); pos += 1
    surface = _lookup(SURFACES, data[pos]); pos += 1
    sac = _lookup(SAC, data[pos]); pos += 1
    flags = data[pos]; pos += 1

    name = None
    if flags & 0x01:
        (n,) = struct.unpack_from("<H", data, pos)
        pos += 2
        name = data[pos : pos + n].decode("utf-8", "replace")
        pos += n
    # v2: flag-guarded, so a v1 file has the bits clear and needs no branch.
    name_en = brand = None
    park_osm_id = None
    for _bit, _which in ((0x02, "name_en"), (0x04, "brand")):
        if flags & _bit:
            (n,) = struct.unpack_from("<H", data, pos)
            pos += 2
            _val = data[pos : pos + n].decode("utf-8", "replace")
            pos += n
            if _which == "name_en":
                name_en = _val
            else:
                brand = _val
    if flags & 0x08:
        park_osm_id, consumed = decode_varint(data, pos)
        pos += consumed

    first_lon, first_lat = coords[0]
    return (
        Trail(
            osm_id=osm_id,
            trail_type=trail_type,
            lat=first_lat,
            lon=first_lon,
            is_point=geom_type == 1,
            surface=surface,
            sac=sac,
            name=name,
            name_en=name_en,
            brand=brand,
            park_osm_id=park_osm_id,
            coords=coords,
        ),
        pos,
        osm_id,
    )


class TrailsReader(MergedBlockReader):
    """Reader for .trails_v1.ptiles files."""

    def decode_records(self, raw: bytes) -> list[Trail]:
        out: list[Trail] = []
        pos = 0
        prev = 0
        while pos < len(raw):
            try:
                trail, pos, prev = decode_trail(raw, pos, prev)
            except (IndexError, struct.error):
                break  # trailing padding, or a record we cannot parse
            out.append(trail)
        return out
