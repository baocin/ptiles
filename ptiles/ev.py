"""Reader for .ev_v1.ptiles files (PTILESE).

EV charging stations from `amenity=charging_station`, built by
scripts/build_ev.py. Published since the 2026-08-07 snapshot with no reader on
this side, so nothing could open it.

Record format, per cell, concatenated:

    varint zigzag osm_id delta (from the previous record in the cell)
    i32  lon, lat        micro-degrees (1e5)
    u8   access index
    u16  power_dkw       deci-kilowatts, so 225 is 22.5 kW; 0 = untagged
    u8   capacity        number of vehicles; 0 = untagged
    u16  sockets         connector bitmask, one bit per SOCKETS entry
    u8   flags           0x01 name, 0x02 network, 0x04 ref
    if flags & 0x01: u16 len + utf-8 name
    if flags & 0x02: u8  len + utf-8 network
    if flags & 0x04: u8  len + utf-8 ref
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ptiles.codec import decode_varint, zigzag_decode
from ptiles.reader import MergedBlockReader

# Order must match scripts/build_ev.py exactly.
ACCESS = ["unknown", "yes", "customers", "permissive", "private", "no"]

# Bit position to connector. The builder's comment is emphatic that this order
# is permanent: inserting in the middle renames every connector on every file
# already published.
SOCKETS = [
    "type1", "type1_combo", "type2", "type2_combo", "type2_cable", "chademo",
    "tesla_supercharger", "tesla_destination", "tesla_supercharger_ccs",
    "nema_5_15", "nema_5_20", "nema_14_50", "schuko",
]

MICRO = 100_000


@dataclass
class Charger:
    osm_id: int
    lat: float
    lon: float
    access: str = "unknown"
    power_kw: float | None = None
    """Rated power. None when OSM carries no usable power tag -- common."""
    capacity: int | None = None
    sockets: list[str] = field(default_factory=list)
    name: str | None = None
    network: str | None = None
    ref: str | None = None


def decode_charger(data: bytes, pos: int, prev_osm_id: int) -> tuple[Charger, int, int]:
    raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(raw)

    lon, lat = struct.unpack_from("<ii", data, pos)
    pos += 8
    access_idx = data[pos]; pos += 1
    (power_dkw,) = struct.unpack_from("<H", data, pos); pos += 2
    capacity = data[pos]; pos += 1
    (socket_mask,) = struct.unpack_from("<H", data, pos); pos += 2
    flags = data[pos]; pos += 1

    name = network = ref = None
    if flags & 0x01:
        (n,) = struct.unpack_from("<H", data, pos)
        pos += 2
        name = data[pos : pos + n].decode("utf-8", "replace")
        pos += n
    if flags & 0x02:
        n = data[pos]; pos += 1
        network = data[pos : pos + n].decode("utf-8", "replace")
        pos += n
    if flags & 0x04:
        n = data[pos]; pos += 1
        ref = data[pos : pos + n].decode("utf-8", "replace")
        pos += n

    return (
        Charger(
            osm_id=osm_id,
            lat=lat / MICRO,
            lon=lon / MICRO,
            access=ACCESS[access_idx] if access_idx < len(ACCESS) else "unknown",
            # 0 means untagged rather than a 0 kW charger.
            power_kw=power_dkw / 10 if power_dkw else None,
            capacity=capacity or None,
            sockets=[s for i, s in enumerate(SOCKETS) if socket_mask & (1 << i)],
            name=name,
            network=network,
            ref=ref,
        ),
        pos,
        osm_id,
    )


class EvReader(MergedBlockReader):
    """Reader for .ev_v1.ptiles files."""

    def decode_records(self, raw: bytes) -> list[Charger]:
        out: list[Charger] = []
        pos = 0
        prev = 0
        while pos < len(raw):
            try:
                charger, pos, prev = decode_charger(raw, pos, prev)
            except (IndexError, struct.error):
                break
            out.append(charger)
        return out
