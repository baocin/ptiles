"""
Composite client for PTiles — PtilesClient.

Opens all available per-state PTiles files and provides a unified
query_point() that returns information across all loaded layers.
Now uses a Layer registry pattern instead of N optional fields.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ptiles.admin import AdminInfo, AdminReader
from ptiles.buildings import Building, BuildingsReader
from ptiles.business import Business, BusinessHit, BusinessReader
from ptiles.camera import Camera, CameraReader
from ptiles.geometry import METERS_PER_DEG_LAT, meters_per_deg_lon
from ptiles.parks import ParkFeature, ParkReader
from ptiles.places import Place, PlacesReader
from ptiles.rail import RailFeature, RailReader
from ptiles.roads import NearestRoad, RoadsReader
from ptiles.signals import Signal, SignalsReader
from ptiles.water import WaterFeature, WaterReader

logger = logging.getLogger("ptiles.composite")


@dataclass
class PointReport:
    building: Building | None = None
    admin: AdminInfo | None = None
    nearest_road: NearestRoad | None = None
    nearby_roads: list[NearestRoad] = field(default_factory=list)
    water: list[WaterFeature] = field(default_factory=list)
    parks: list[ParkFeature] = field(default_factory=list)
    places: list[Place] = field(default_factory=list)
    businesses: list[BusinessHit] = field(default_factory=list)
    rail: list[RailFeature] = field(default_factory=list)
    cameras: list[Camera] = field(default_factory=list)
    """Cameras within range, whether or not they are aimed here."""
    cameras_seeing: list = field(default_factory=list)
    """Subset aimed at this point — ptiles.visibility.Sighting, each carrying
    the assumptions it was judged under."""
    signals: list[Signal] = field(default_factory=list)
    sun: object | None = None
    """ptiles.sun.SunPosition, when the admin layer supplied a timezone."""
    shade: object | None = None
    """ptiles.sun.ShadeResult. Its `shaded` is None when unknown — with height
    on a minority of footprints that is a common and meaningful answer."""
    local_time: object | None = None
    """Wall-clock time at this location, from the admin layer's timezone."""


@dataclass
class CorridorReport:
    buildings: list[Building] = field(default_factory=list)
    business: list[Business] = field(default_factory=list)
    roads: list = field(default_factory=list)
    parks: list[ParkFeature] = field(default_factory=list)
    water: list[WaterFeature] = field(default_factory=list)
    rail: list[RailFeature] = field(default_factory=list)
    places: list[Place] = field(default_factory=list)
    cameras: list[Camera] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)


class Layer(Protocol):
    """A queryable geographic layer. Each layer implements query_point()."""

    def query_point(
        self, lat: float, lon: float, report: PointReport, **kw
    ) -> None: ...

    def query_corridor(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        report: CorridorReport,
        limit: int,
    ) -> None: ...

    def close(self) -> None: ...


# --- Adapters wrapping each reader as a Layer ---


class BuildingLayer:
    def __init__(self, path):
        self._reader = BuildingsReader.open(path)

    def query_point(self, lat, lon, report, **kw):
        try:
            report.building = self._reader.query(lat, lon)
        except Exception as e:
            logger.warning("Buildings query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.buildings = self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            )
        except Exception as e:
            logger.warning("Corridor buildings failed: %s", e)

    def close(self):
        self._reader.close()


class AdminLayer:
    def __init__(self, path):
        self._reader = AdminReader.open(path)

    def query_point(self, lat, lon, report, **kw):
        try:
            report.admin = self._reader.query(lat, lon)
        except Exception as e:
            logger.warning("Admin query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        """No-op: admin is a per-cell lookup grid, not a feature list.

        Present so `corridor()` can call every layer uniformly.
        """

    def close(self):
        self._reader.close()


class RoadLayer:
    def __init__(self, path):
        self._reader = RoadsReader.open(path)

    def query_point(self, lat, lon, report, **kw):
        try:
            report.nearest_road = self._reader.nearest(lat, lon)
            report.nearby_roads = self._reader.nearest_n(lat, lon, n=5)
        except Exception as e:
            logger.warning("Roads nearest query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.roads = self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            )
        except Exception as e:
            logger.warning("Corridor roads failed: %s", e)

    def close(self):
        self._reader.close()


class WaterLayer:
    def __init__(self, path):
        self._reader = WaterReader.open(path)

    def query_point(self, lat, lon, report, **kw):
        try:
            import h3

            cell = int(h3.latlng_to_cell(lat, lon, 7), 16)
            report.water = self._reader.get_in_cell(cell)
        except Exception as e:
            logger.warning("Water query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.water = self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            )
        except Exception as e:
            logger.warning("Corridor water failed: %s", e)

    def close(self):
        self._reader.close()


class ParkLayer:
    def __init__(self, path):
        self._reader = ParkReader.open(path)

    def query_point(self, lat, lon, report, **kw):
        try:
            import h3

            cell = int(h3.latlng_to_cell(lat, lon, 7), 16)
            report.parks = self._reader.get_in_cell(cell)
        except Exception as e:
            logger.warning("Parks query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.parks = self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            )
        except Exception as e:
            logger.warning("Corridor parks failed: %s", e)

    def close(self):
        self._reader.close()


class RailLayer:
    def __init__(self, path):
        self._reader = RailReader.open(path)

    def query_point(self, lat, lon, report, **kw):
        try:
            import h3

            cell = int(h3.latlng_to_cell(lat, lon, 7), 16)
            report.rail = self._reader.get_in_cell(cell)
        except Exception as e:
            logger.warning("Rail query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.rail = self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            )
        except Exception as e:
            logger.warning("Corridor rail failed: %s", e)

    def close(self):
        self._reader.close()


class PlaceLayer:
    def __init__(self, path):
        self._reader = PlacesReader.open(path)

    def query_point(self, lat, lon, report, **kw):
        try:
            report.places = self._reader.get_in_bounds(
                lat - 0.05, lon - 0.05, lat + 0.05, lon + 0.05, limit=20
            )
        except Exception as e:
            logger.warning("Places query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.places = self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            )
        except Exception as e:
            logger.warning("Corridor places failed: %s", e)

    def close(self):
        self._reader.close()


class CameraLayer:
    def __init__(self, path):
        self._reader = CameraReader.open(path)

    def query_point(self, lat, lon, report, **kw):
        try:
            from ptiles.visibility import cameras_near, cameras_seeing

            radius = kw.get("camera_radius_meters", 200)
            report.cameras = [s.camera for s in
                              cameras_near(lat, lon, self._reader, max_meters=radius)]
            report.cameras_seeing = cameras_seeing(
                lat, lon, self._reader,
                buildings=kw.get("buildings_reader"),
            )
        except Exception as e:
            logger.warning("Camera query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.cameras = self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            )
        except Exception as e:
            logger.warning("Corridor camera failed: %s", e)

    def close(self):
        self._reader.close()


class SignalLayer:
    def __init__(self, path):
        self._reader = SignalsReader.open(path)

    def query_point(self, lat, lon, report, **kw):
        try:
            from ptiles.nearest import nearest

            found = nearest(self._reader, lat, lon, n=kw.get("signal_limit", 10),
                            max_meters=kw.get("signal_radius_meters", 250))
            report.signals = [h.feature for h in found]
        except Exception as e:
            logger.warning("Signals query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.signals = self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            )
        except Exception as e:
            logger.warning("Corridor signals failed: %s", e)

    def close(self):
        self._reader.close()


class BusinessLayer:
    def __init__(self, path):
        self._reader = BusinessReader.open(path)

    def query_point(self, lat, lon, report, **kw):
        try:
            report.businesses = self._reader.nearby(
                lat,
                lon,
                radius_meters=kw.get("business_radius_meters", 500),
                limit=kw.get("business_limit", 5),
            )
        except Exception as e:
            logger.warning("Business nearby failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.business = self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            )
        except Exception as e:
            logger.warning("Corridor business failed: %s", e)

    def close(self):
        self._reader.close()


# --- Config: suffix -> (layer_factory, include_in_point, include_in_corridor) ---

LAYER_CONFIG: list[tuple[str, type, bool, bool]] = [
    ("buildings_v8", BuildingLayer, True, True),
    ("roads", RoadLayer, True, True),
    ("water", WaterLayer, True, True),
    ("business", BusinessLayer, True, True),
    ("places", PlaceLayer, True, True),
    ("parks", ParkLayer, True, True),
    ("rail", RailLayer, True, True),
    ("camera", CameraLayer, True, True),
    ("signals", SignalLayer, True, True),
]

# Layers published as one national file rather than per state, so open_state
# looks for them under US.* instead of <STATE>.*.
US_WIDE_LAYERS: dict[str, type] = {
    "admin": AdminLayer,
    "camera": CameraLayer,
    "signals": SignalLayer,
}

# Filename variants seen in the published sets. buildings ship as
# `<ST>.buildings_v9.ptiles` while LAYER_CONFIG still keys them `buildings_v8`,
# and business as `<ST>.business_v4.ptiles` — open_state tries each in order so
# a v4 download works without renaming.
LAYER_FILE_ALIASES: dict[str, tuple[str, ...]] = {
    "buildings_v8": ("buildings_v9", "buildings_v8", "buildings"),
    "business": ("business_v4", "business"),
    "roads": ("highways_v2", "roads"),
    "water": ("water_v1", "water"),
    "places": ("places_v1", "places"),
    "parks": ("parks_v1", "parks"),
    "rail": ("rail_v1", "rail"),
}


class PtilesClient:
    """Composite PTiles client that opens all layers for a given state."""

    def __init__(self):
        self._layers: list[Layer] = []
        self._buildings = None
        self._admin = None

    @classmethod
    def open(
        cls,
        *,
        buildings: str | os.PathLike | None = None,
        roads: str | os.PathLike | None = None,
        water: str | os.PathLike | None = None,
        admin: str | os.PathLike | None = None,
        places: str | os.PathLike | None = None,
        rail: str | os.PathLike | None = None,
        parks: str | os.PathLike | None = None,
        business: str | os.PathLike | None = None,
        camera: str | os.PathLike | None = None,
        signals: str | os.PathLike | None = None,
    ) -> "PtilesClient":
        """Open specific layer files."""
        client = cls()
        path_map = dict(
            buildings_v8=buildings,
            roads=roads,
            water=water,
            business=business,
            places=places,
            parks=parks,
            rail=rail,
            camera=camera,
            signals=signals,
        )
        for suffix, factory, *_ in LAYER_CONFIG:
            p = path_map.get(suffix)
            if p:
                client._layers.append(factory(p))
        if admin:
            client._layers.append(AdminLayer(admin))
        client._wire_cross_layer()
        return client

    @classmethod
    def open_state(cls, state: str, data_dir: str | os.PathLike) -> "PtilesClient":
        """Open all available <STATE>.<layer>.ptiles files in data_dir."""
        state_upper = (
            state.upper()
            if not state.startswith("US.")
            else state[:2].upper() + state[2:]
        )
        data_dir = Path(data_dir)
        client = cls()

        for suffix, factory, *_ in LAYER_CONFIG:
            if suffix in US_WIDE_LAYERS:
                continue
            for alias in LAYER_FILE_ALIASES.get(suffix, (suffix,)):
                p = data_dir / f"{state_upper}.{alias}.ptiles"
                if p.exists():
                    client._layers.append(factory(p))
                    break

        # National files, which are not per-state and so are never prefixed
        # with the state abbreviation.
        for suffix, factory in US_WIDE_LAYERS.items():
            p = data_dir / f"US.{suffix}.ptiles"
            if p.exists():
                client._layers.append(factory(p))

        client._wire_cross_layer()
        return client

    def _wire_cross_layer(self) -> None:
        """Note the readers that other layers need.

        Occlusion needs footprints and shading needs both footprints and a
        timezone, so those two answers cannot come from a single layer in
        isolation the way the rest can.
        """
        self._buildings = None
        self._admin = None
        for layer in self._layers:
            if isinstance(layer, BuildingLayer):
                self._buildings = layer._reader
            elif isinstance(layer, AdminLayer):
                self._admin = layer._reader

    def query_point(
        self,
        lat: float,
        lon: float,
        *,
        include_buildings: bool = True,
        include_admin: bool = True,
        include_nearest_road: bool = True,
        nearby_business_limit: int = 5,
        nearby_business_radius_meters: float = 500,
        water_radius_meters: float = 100,
        camera_radius_meters: float = 200,
        check_occlusion: bool = False,
        include_sun: bool = True,
        at: "datetime | None" = None,
    ) -> PointReport:
        """Query all loaded layers at a single point.

        Args:
            check_occlusion: test camera sightlines against building
                footprints. Off by default — it costs a footprint read per
                camera and is only a plan-view approximation.
            include_sun: compute sun position and shade when both the
                buildings and admin layers are loaded.
            at: instant to evaluate the sun at. Defaults to now.
        """
        report = PointReport()
        kw = dict(
            business_limit=nearby_business_limit,
            business_radius_meters=nearby_business_radius_meters,
            water_radius_meters=water_radius_meters,
            camera_radius_meters=camera_radius_meters,
            buildings_reader=self._buildings if check_occlusion else None,
        )
        for layer in self._layers:
            layer.query_point(lat, lon, report, **kw)

        if include_sun and self._buildings is not None:
            try:
                from datetime import datetime, timezone
                from ptiles.sun import is_shaded, local_time, sun_position

                when = at or datetime.now(timezone.utc)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if self._admin is not None:
                    # Resolve local time so a caller printing the result sees
                    # the wall clock at the location, not UTC.
                    report.local_time = local_time(lat, lon, self._admin, when)
                report.sun = sun_position(lat, lon, when)
                report.shade = is_shaded(lat, lon, self._buildings, when)
            except Exception as e:
                logger.warning("Sun/shade query failed: %s", e)

        return report

    def corridor(
        self,
        path: list[tuple[float, float]],
        buffer_meters: float,
        *,
        layers: list[str] | None = None,
        limit_per_layer: int = 5000,
    ) -> CorridorReport:
        """Query features that intersect a buffered route corridor.

        `path` is a list of (lon, lat) pairs.
        """
        # Resolve the requested layer names to adapter classes. This used to
        # compute the list and then ignore it, so `layers=` had no effect and
        # every loaded layer was queried regardless.
        wanted: set[type] | None = None
        if layers is not None:
            by_name = {suffix: factory for suffix, factory, *_ in LAYER_CONFIG}
            unknown = [name for name in layers if name not in by_name]
            if unknown:
                raise ValueError(
                    f"unknown corridor layer(s): {', '.join(unknown)}; "
                    f"known: {', '.join(sorted(by_name))}"
                )
            wanted = {by_name[name] for name in layers}

        lats = [p[1] for p in path]
        lons = [p[0] for p in path]
        # Pad by the buffer, scaling longitude at the corridor's own latitude
        # rather than by a fixed factor — 0.76 is only right near 40 deg, and
        # under-buffers the box everywhere north of that.
        mid_lat = (min(lats) + max(lats)) / 2.0
        pad_lat = buffer_meters / METERS_PER_DEG_LAT
        pad_lon = buffer_meters / meters_per_deg_lon(mid_lat)
        min_lat = min(lats) - pad_lat
        max_lat = max(lats) + pad_lat
        min_lon = min(lons) - pad_lon
        max_lon = max(lons) + pad_lon

        report = CorridorReport()
        for layer in self._layers:
            if wanted is not None and type(layer) not in wanted:
                continue
            layer.query_corridor(
                min_lat, min_lon, max_lat, max_lon, report, limit_per_layer
            )
        return report

    # Which layer answers a bare `nearest(..., what=...)` keyword.
    _NEAREST_LAYERS: dict[str, tuple[type, str, tuple[str, ...]]] = {
        "camera": (CameraLayer, "", ()),
        "alpr": (CameraLayer, "device_type", ("ALPR",)),
        "signal": (SignalLayer, "signal_type", ("traffic_signals",)),
        "stop": (SignalLayer, "signal_type", ("stop",)),
        "river": (WaterLayer, "water_type", ("river", "stream")),
        "lake": (WaterLayer, "water_type", ("lake", "pond", "reservoir")),
        "water": (WaterLayer, "", ()),
        "park": (ParkLayer, "", ()),
        "rail": (RailLayer, "", ()),
        "place": (PlaceLayer, "", ()),
    }

    def nearest(self, lat: float, lon: float, what: str, *, n: int = 1,
                max_meters: float | None = None) -> list:
        """Nearest features matching `what`, nearest first.

        `what` is either a layer keyword — camera, alpr, signal, stop, river,
        lake, water, park, rail, place — or anything else, which is taken as a
        business name or brand and looked up in the business layer. So
        "Taco Bell" and "river" both work.

        Returns ptiles.nearest.NearestHit objects.
        """
        from ptiles.nearest import match_attr, match_name, nearest as _nearest

        key = what.strip().lower()
        if key in self._NEAREST_LAYERS:
            layer_cls, attr, values = self._NEAREST_LAYERS[key]
            layer = next((l for l in self._layers if isinstance(l, layer_cls)), None)
            if layer is None:
                raise LookupError(f"no layer loaded that can answer {what!r}")
            predicate = match_attr(attr, *values) if attr else None
        else:
            layer = next((l for l in self._layers
                          if isinstance(l, BusinessLayer)), None)
            if layer is None:
                raise LookupError(
                    f"{what!r} is not a layer keyword and no business layer is "
                    f"loaded to search by name"
                )
            predicate = match_name(what)

        return list(_nearest(layer._reader, lat, lon, predicate=predicate, n=n,
                             max_meters=max_meters))

    def close(self) -> None:
        """Close all open readers."""
        for layer in self._layers:
            try:
                layer.close()
            except Exception:
                pass
