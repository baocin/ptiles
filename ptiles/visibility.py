"""
Inverse line of sight: of the cameras around this point, which are aimed at it.

Proximity is the easy half and the misleading one — a plate reader 20 m away
pointing down the other carriageway does not see you, and a dome unit 60 m away
does. What the layer supports is a *bearing* test on 72.4% of devices, plus an
optional 2-D occlusion test against building footprints.

Two things the data does not contain, which callers are asked to supply rather
than have guessed silently:

* **Range.** No field exists. The defaults below come from what the device is
  for — a plate reader has to resolve characters, a general camera only has to
  resolve a shape — not from anything measured. Override per deployment.
* **Field of view.** Only 299 of 129,251 devices state one (0.2%). Everything
  else falls back to a per-type default, which is why a hit reports how it was
  judged instead of a bare yes.

Nothing here reasons in three dimensions. There is no camera mounting height
in the format, and building height is present for 0.3% of footprints, so a
camera shooting over a low wall reads as blocked and one on a roof reads as
ground level. Occlusion is therefore off by default and advisory when on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from ptiles.camera import Camera
from ptiles.geometry import (
    angular_difference,
    bearing_degrees,
    segment_crosses_ring,
)
from ptiles.nearest import nearest

logger = logging.getLogger("ptiles.visibility")

# Effective range in metres, by device_type. Assumptions, not data.
DEFAULT_RANGE_M = {
    "ALPR": 50.0,      # must resolve plate characters
    "camera": 80.0,    # general surveillance
    "guard": 30.0,     # a person, with a person's useful range
    "unknown": 60.0,
}

# Field of view in degrees, by camera_type, used when the record omits `angle`.
# Dome and panning units sweep, so they are treated as covering everything
# within range regardless of any stored bearing.
DEFAULT_FOV_DEG = {
    "fixed": 60.0,
    "dome": 360.0,
    "panning": 360.0,
    "unknown": 90.0,
}

# Widest ring the camera search will spiral to, in metres. Past the largest
# plausible range there is no point reading more cells.
MAX_SEARCH_M = 250.0


class Confidence(str, Enum):
    """How much the answer rests on data versus assumption."""

    CERTAIN = "certain"
    """Bearing and field of view both stated by the record."""

    ASSUMED = "assumed"
    """Bearing stated, field of view defaulted from camera_type."""

    OMNIDIRECTIONAL = "omnidirectional"
    """No usable bearing — either absent, or a dome/panning unit that sweeps.
    Included because it plausibly sees the point, not because it is aimed."""


@dataclass(frozen=True, slots=True)
class Sighting:
    camera: Camera
    distance_meters: float
    bearing_from_camera: float
    """Compass bearing from the camera to the query point."""
    off_axis_degrees: float | None
    """How far off the camera's stated aim the point sits. None if no bearing."""
    assumed_range_m: float
    assumed_fov_deg: float
    confidence: Confidence
    occluded_by: int | None = None
    """OSM id of a building footprint crossing the sightline, if checked."""

    @property
    def is_occluded(self) -> bool:
        return self.occluded_by is not None


def _range_for(camera: Camera, overrides: dict[str, float] | None) -> float:
    table = {**DEFAULT_RANGE_M, **(overrides or {})}
    return table.get(camera.device_type, table["unknown"])


def _fov_for(camera: Camera, overrides: dict[str, float] | None) -> tuple[float, bool]:
    """(field of view in degrees, whether the record stated it)."""
    if camera.angle is not None:
        return float(camera.angle), True
    table = {**DEFAULT_FOV_DEG, **(overrides or {})}
    return table.get(camera.camera_type, table["unknown"]), False


def cameras_seeing(
    lat: float,
    lon: float,
    camera_reader,
    *,
    buildings=None,
    max_meters: float | None = None,
    range_overrides: dict[str, float] | None = None,
    fov_overrides: dict[str, float] | None = None,
    include_occluded: bool = False,
    limit: int = 200,
) -> list[Sighting]:
    """Cameras whose cone plausibly contains (lat, lon), nearest first.

    Args:
        camera_reader: an open CameraReader.
        buildings: optional BuildingsReader. When given, each sightline is
            tested against nearby footprints in plan view; see the module note
            on why that is advisory.
        max_meters: hard cap on distance. Defaults to each device's assumed
            range, which is the more meaningful bound.
        range_overrides: per device_type range in metres.
        fov_overrides: per camera_type field of view in degrees.
        include_occluded: keep sightings a building blocks, flagged rather
            than dropped.
        limit: cap on cameras considered.

    Returns:
        Sightings, nearest first. Each carries the assumptions it was judged
        under, so a caller can present "3 cameras, 1 certain" honestly.
    """
    search_radius = max_meters if max_meters is not None else MAX_SEARCH_M
    found = nearest(
        camera_reader, lat, lon,
        n=limit,
        max_meters=search_radius,
    )

    sightings: list[Sighting] = []
    for hit in found:
        camera: Camera = hit.feature
        reach = _range_for(camera, range_overrides)
        if max_meters is not None:
            reach = min(reach, max_meters)
        if hit.distance_meters > reach:
            continue

        fov, fov_stated = _fov_for(camera, fov_overrides)
        to_point = bearing_degrees(camera.lat, camera.lon, lat, lon)

        if camera.is_directional:
            off_axis = angular_difference(camera.direction, to_point)
            if off_axis > fov / 2.0:
                continue
            confidence = Confidence.CERTAIN if fov_stated else Confidence.ASSUMED
        else:
            # No bearing, or a unit that sweeps: within range is the whole test.
            off_axis = (angular_difference(camera.direction, to_point)
                        if camera.direction is not None else None)
            confidence = Confidence.OMNIDIRECTIONAL

        occluded_by = None
        if buildings is not None:
            occluded_by = _first_blocker(
                camera.lat, camera.lon, lat, lon, buildings, hit.distance_meters,
            )
            if occluded_by is not None and not include_occluded:
                continue

        sightings.append(Sighting(
            camera=camera,
            distance_meters=hit.distance_meters,
            bearing_from_camera=to_point,
            off_axis_degrees=off_axis,
            assumed_range_m=reach,
            assumed_fov_deg=fov,
            confidence=confidence,
            occluded_by=occluded_by,
        ))

    sightings.sort(key=lambda s: s.distance_meters)
    return sightings


def _first_blocker(cam_lat: float, cam_lon: float,
                   lat: float, lon: float,
                   buildings, distance_m: float) -> int | None:
    """OSM id of the first footprint crossing the sightline, in plan view.

    A footprint containing either endpoint is skipped: standing next to a
    building, or a camera mounted on one, is not the same as that building
    being in the way.
    """
    a = (cam_lon, cam_lat)
    b = (lon, lat)
    try:
        candidates = buildings.within(lat, lon, distance_m + 50.0)
    except Exception as e:
        logger.warning("occlusion check skipped: %s", e)
        return None

    for bldg in candidates:
        ring = bldg.coordinates
        if len(ring) < 3:
            continue
        if segment_crosses_ring(a, b, ring):
            return bldg.osm_id
    return None


def cameras_near(lat: float, lon: float, camera_reader, *,
                 max_meters: float = 200.0, limit: int = 50) -> list[Sighting]:
    """Every camera within range, aimed at the point or not.

    Use when the question is "what is around me" rather than "what sees me" —
    the returned sightings still carry bearing and off-axis angle so a caller
    can render which way each one is looking.
    """
    found = nearest(camera_reader, lat, lon, n=limit, max_meters=max_meters)
    out: list[Sighting] = []
    for hit in found:
        camera: Camera = hit.feature
        fov, fov_stated = _fov_for(camera, None)
        to_point = bearing_degrees(camera.lat, camera.lon, lat, lon)
        off_axis = (angular_difference(camera.direction, to_point)
                    if camera.direction is not None else None)
        out.append(Sighting(
            camera=camera,
            distance_meters=hit.distance_meters,
            bearing_from_camera=to_point,
            off_axis_degrees=off_axis,
            assumed_range_m=_range_for(camera, None),
            assumed_fov_deg=fov,
            confidence=(Confidence.CERTAIN if (camera.is_directional and fov_stated)
                        else Confidence.ASSUMED if camera.is_directional
                        else Confidence.OMNIDIRECTIONAL),
        ))
    return out
