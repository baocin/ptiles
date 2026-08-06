"""
Camera / surveillance reader for PTiles (US.camera.ptiles, magic PTILESC).

OSM `man_made=surveillance` nodes. Measured against the shipped national file
(129,251 devices):

    ALPR                 74.7%     bearing present   72.4%
    plain camera         20.6%     operator present  19.7%
    guard                 0.1%     name present       2.4%
    unknown               4.6%     FOV angle present  0.2%

So the layer is mostly automated plate readers, most of them with a usable
bearing and almost none with a stated field of view. Anything reasoning about
what a camera can see has to supply its own cone — see ptiles.visibility.

Record layout mirrors `enc_camera` in scripts/build_points.py:

    osm_id       zigzag varint, delta from previous record in the cell
    lon, lat     i32 microdegrees
    device_type  u8
    placement    u8
    camera_type  u8
    flags        u8
    [direction]  u16   if flags & 0x01, degrees 0-359
    [operator]   u8len if flags & 0x02
    [name]       u16len if flags & 0x04
    [ref]        u8len if flags & 0x08
    [angle]      u8    if flags & 0x10, FOV degrees 0-180
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ptiles.codec import decode_string_u8, decode_string_u16, decode_varint, zigzag_decode
from ptiles.points import PointLayerReader

DEVICE_TYPES = ("camera", "ALPR", "guard", "unknown")
PLACEMENTS = ("public", "outdoor", "indoor", "unknown")
CAMERA_TYPES = ("fixed", "panning", "dome", "unknown")


def _index(table: tuple[str, ...], i: int) -> str:
    return table[i] if i < len(table) else "unknown"


@dataclass(frozen=True, slots=True)
class Camera:
    osm_id: int
    lat: float
    lon: float
    device_type: str          # camera | ALPR | guard | unknown
    placement: str            # public | outdoor | indoor | unknown
    camera_type: str          # fixed | panning | dome | unknown
    direction: int | None = None   # bearing in degrees, 0 = north
    angle: int | None = None       # field of view in degrees
    operator: str | None = None
    name: str | None = None
    ref: str | None = None

    @property
    def is_alpr(self) -> bool:
        return self.device_type == "ALPR"

    @property
    def is_directional(self) -> bool:
        """Does this camera have a bearing that constrains what it sees?

        Dome and panning units sweep, so their stored bearing (if any) does
        not limit coverage.
        """
        return self.direction is not None and self.camera_type not in ("dome", "panning")


def decode_camera(data: bytes, pos: int, prev_osm_id: int) -> tuple[Camera, int, int]:
    """Decode one camera record. Returns (camera, bytes_consumed, osm_id)."""
    start = pos

    raw, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + zigzag_decode(raw)

    lon_micro, lat_micro = struct.unpack_from("<ii", data, pos)
    pos += 8

    device_type = _index(DEVICE_TYPES, data[pos])
    placement = _index(PLACEMENTS, data[pos + 1])
    camera_type = _index(CAMERA_TYPES, data[pos + 2])
    flags = data[pos + 3]
    pos += 4

    direction = None
    operator = name = ref = None
    angle = None

    if flags & 0x01:
        direction = struct.unpack_from("<H", data, pos)[0]
        pos += 2
    if flags & 0x02:
        operator, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x04:
        name, consumed = decode_string_u16(data, pos)
        pos += consumed
    if flags & 0x08:
        ref, consumed = decode_string_u8(data, pos)
        pos += consumed
    if flags & 0x10:
        angle = data[pos]
        pos += 1

    camera = Camera(
        osm_id=osm_id,
        lat=lat_micro / 100_000,
        lon=lon_micro / 100_000,
        device_type=device_type,
        placement=placement,
        camera_type=camera_type,
        direction=direction,
        angle=angle,
        operator=operator or None,
        name=name or None,
        ref=ref or None,
    )
    return camera, pos - start, osm_id


class CameraReader(PointLayerReader):
    """Reader for US.camera.ptiles."""

    magic = b"PTILESC"
    decode_record = staticmethod(decode_camera)
