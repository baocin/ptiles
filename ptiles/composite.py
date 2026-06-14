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
from pathlib import Path
from typing import Protocol

from ptiles.admin import AdminInfo, AdminReader
from ptiles.buildings import Building, BuildingsReader
from ptiles.business import Business, BusinessHit, BusinessReader
from ptiles.parks import ParkFeature, ParkReader
from ptiles.places import Place, PlacesReader
from ptiles.roads import NearestRoad, RoadsReader
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


@dataclass
class CorridorReport:
    buildings: list[Building] = field(default_factory=list)
    business: list[Business] = field(default_factory=list)
    roads: list = field(default_factory=list)
    parks: list[ParkFeature] = field(default_factory=list)
    water: list[WaterFeature] = field(default_factory=list)


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
    # ("rail", RailLayer, True, True),  # no Layer adapter yet
]


class PtilesClient:
    """Composite PTiles client that opens all layers for a given state."""

    def __init__(self):
        self._layers: list[Layer] = []

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
        )
        for suffix, factory, *_ in LAYER_CONFIG:
            p = path_map.get(suffix)
            if p:
                client._layers.append(factory(p))
        if admin:
            client._layers.append(AdminLayer(admin))
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

        def state_path(suffix: str) -> Path:
            return data_dir / f"{state_upper}.{suffix}.ptiles"

        client = cls()

        for suffix, factory, *_ in LAYER_CONFIG:
            p = state_path(suffix)
            if p.exists():
                client._layers.append(factory(p))

        # US-wide admin file
        admin_path = data_dir / "US.admin.ptiles"
        if admin_path.exists():
            client._layers.append(AdminLayer(admin_path))

        return client

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
    ) -> PointReport:
        """Query all loaded layers at a single point."""
        report = PointReport()
        kw = dict(
            business_limit=nearby_business_limit,
            business_radius_meters=nearby_business_radius_meters,
            water_radius_meters=water_radius_meters,
        )
        for layer in self._layers:
            layer.query_point(lat, lon, report, **kw)
        return report

    def corridor(
        self,
        path: list[tuple[float, float]],
        buffer_meters: float,
        *,
        layers: list[str] | None = None,
        limit_per_layer: int = 5000,
    ) -> CorridorReport:
        """Query features that intersect a buffered route corridor."""
        if layers is None:
            layer_names = [s for s, _, _, in_cl in LAYER_CONFIG if in_cl]
        else:
            layer_names = layers

        lats = [p[1] for p in path]
        lons = [p[0] for p in path]
        min_lat = min(lats) - (buffer_meters / 111_320)
        max_lat = max(lats) + (buffer_meters / 111_320)
        min_lon = min(lons) - (buffer_meters / (111_320 * 0.76))
        max_lon = max(lons) + (buffer_meters / (111_320 * 0.76))

        report = CorridorReport()
        for layer in self._layers:
            layer.query_corridor(
                min_lat, min_lon, max_lat, max_lon, report, limit_per_layer
            )
        return report

    def close(self) -> None:
        """Close all open readers."""
        for layer in self._layers:
            try:
                layer.close()
            except Exception:
                pass
