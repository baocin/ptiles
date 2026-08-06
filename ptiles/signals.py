"""
Traffic-control reader for PTiles (US.signals.ptiles, magic PTILESS).

OSM `highway=traffic_signals` / `stop` / `give_way` and crossing/railway
signals. Measured against the shipped national file (2,107,809 nodes):

    stop              46.5%
    crossing_signals  25.5%
    traffic_signals   25.4%
    give_way           2.6%
    railway_signals     ~0%

    bearing present    0.1%   (2,310 of 2,107,809)

The bearing is in the format but effectively absent from the data, so this
layer supports "what control is at this junction" and per-junction delay
estimates, and does not support approach-direction or turn-restriction
reasoning.

Record layout mirrors `enc_signal` in scripts/build_points.py:

    osm_id       zigzag varint, delta from previous record in the cell
    lon, lat     i32 microdegrees
    signal_type  u8
    flags        u8
    [direction]  u16  if flags & 0x01
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ptiles.codec import decode_varint, zigzag_decode
from ptiles.points import PointLayerReader

SIGNAL_TYPES = ("traffic_signals", "crossing_signals", "stop", "give_way",
                "railway_signals")

# Seconds of expected delay, matching RoadsReader's IntersectionType table so
# routing costs agree between the two layers.
SIGNAL_DELAY_SECONDS = {
    "traffic_signals": 20.0,
    "crossing_signals": 10.0,
    "stop": 4.0,
    "give_way": 3.0,
    "railway_signals": 20.0,
}


@dataclass(frozen=True, slots=True)
class Signal:
    osm_id: int
    lat: float
    lon: float
    signal_type: str
    direction: int | None = None

    @property
    def delay_seconds(self) -> float:
        return SIGNAL_DELAY_SECONDS.get(self.signal_type, 0.0)


def decode_signal(data: bytes, pos: int, prev_osm_id: int) -> tuple[Signal, int, int]:
    """Decode one signal record. Returns (signal, bytes_consumed, osm_id)."""
    start = pos

    raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(raw)

    lon_micro, lat_micro = struct.unpack_from("<ii", data, pos)
    pos += 8

    type_idx = data[pos]
    flags = data[pos + 1]
    pos += 2

    direction = None
    if flags & 0x01:
        direction = struct.unpack_from("<H", data, pos)[0]
        pos += 2

    signal = Signal(
        osm_id=osm_id,
        lat=lat_micro / 100_000,
        lon=lon_micro / 100_000,
        signal_type=(SIGNAL_TYPES[type_idx] if type_idx < len(SIGNAL_TYPES)
                     else "unknown"),
        direction=direction,
    )
    return signal, pos - start, osm_id


class SignalsReader(PointLayerReader):
    """Reader for US.signals.ptiles."""

    magic = b"PTILESS"
    decode_record = staticmethod(decode_signal)
