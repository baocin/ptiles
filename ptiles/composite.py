"""
Composite client for PTiles — PtilesClient.

Opens all available per-state PTiles files and provides a unified
query_point() that returns information across all loaded layers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ptiles import PTilesError
from ptiles.admin import AdminInfo, AdminReader
from ptiles.buildings import Building, BuildingsReader
from ptiles.business import Business, BusinessHit, BusinessReader
from ptiles.parks import ParkFeature, ParkReader
from ptiles.places import Place, PlacesReader
from ptiles.rail import RailFeature, RailReader
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


class PtilesClient:
    """Composite PTiles client that opens all layers for a given state."""

    def __init__(self):
        self.buildings: BuildingsReader | None = None
        self.roads: RoadsReader | None = None
        self.water: WaterReader | None = None
        self.admin: AdminReader | None = None
        self.places: PlacesReader | None = None
        self.rail: RailReader | None = None
        self.parks: ParkReader | None = None
        self.business: BusinessReader | None = None

    @classmethod
    def open(cls, *,
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
        if buildings:
            client.buildings = BuildingsReader.open(buildings)
        if roads:
            client.roads = RoadsReader.open(roads)
        if water:
            client.water = WaterReader.open(water)
        if admin:
            client.admin = AdminReader.open(admin)
        if places:
            client.places = PlacesReader.open(places)
        if rail:
            client.rail = RailReader.open(rail)
        if parks:
            client.parks = ParkReader.open(parks)
        if business:
            client.business = BusinessReader.open(business)
        return client

    @classmethod
    def open_state(cls, state: str, data_dir: str | os.PathLike) -> "PtilesClient":
        """Open all available <STATE>.<layer>.ptiles files in data_dir."""
        state_upper = state.upper() if not state.startswith("US.") else state[:2].upper() + state[2:]
        data_dir = Path(data_dir)

        def state_path(suffix: str) -> Path:
            return data_dir / f"{state_upper}.{suffix}.ptiles"

        client = cls()

        buildings_path = state_path("buildings_v8")
        if buildings_path.exists():
            client.buildings = BuildingsReader.open(buildings_path)

        roads_path = state_path("roads")
        if roads_path.exists():
            client.roads = RoadsReader.open(roads_path)

        water_path = state_path("water")
        if water_path.exists():
            client.water = WaterReader.open(water_path)

        business_path = state_path("business")
        if business_path.exists():
            client.business = BusinessReader.open(business_path)

        places_path = state_path("places")
        if places_path.exists():
            client.places = PlacesReader.open(places_path)

        rail_path = state_path("rail")
        if rail_path.exists():
            client.rail = RailReader.open(rail_path)

        parks_path = state_path("parks")
        if parks_path.exists():
            client.parks = ParkReader.open(parks_path)

        # US-wide admin file
        admin_path = data_dir / "US.admin.ptiles"
        if admin_path.exists():
            client.admin = AdminReader.open(admin_path)

        return client

    def query_point(self, lat: float, lon: float, *,
                    include_buildings: bool = True,
                    include_admin: bool = True,
                    include_nearest_road: bool = True,
                    nearby_business_limit: int = 5,
                    nearby_business_radius_meters: float = 500,
                    water_radius_meters: float = 100,
                    ) -> PointReport:
        """Query all loaded layers at a single point.

        Returns a PointReport with all available info.
        """
        report = PointReport()

        if include_admin and self.admin:
            report.admin = self.admin.query(lat, lon)

        if include_buildings and self.buildings:
            try:
                report.building = self.buildings.query(lat, lon)
            except Exception as e:
                logger.warning("Buildings query failed: %s", e)

        if include_nearest_road and self.roads:
            try:
                report.nearest_road = self.roads.nearest(lat, lon)
                report.nearby_roads = self.roads.nearest_n(lat, lon, n=5)
            except Exception as e:
                logger.warning("Roads nearest query failed: %s", e)

        if self.water:
            try:
                cell = self._latlng_to_cell_int(lat, lon)
                report.water = self.water.get_in_cell(cell)
            except Exception as e:
                logger.warning("Water query failed: %s", e)

        if self.parks:
            try:
                cell = self._latlng_to_cell_int(lat, lon)
                report.parks = self.parks.get_in_cell(cell)
            except Exception as e:
                logger.warning("Parks query failed: %s", e)

        if self.places:
            try:
                report.places = self.places.get_in_bounds(
                    lat - 0.05, lon - 0.05, lat + 0.05, lon + 0.05, limit=20
                )
            except Exception as e:
                logger.warning("Places query failed: %s", e)

        if self.business:
            try:
                report.businesses = self.business.nearby(
                    lat, lon,
                    radius_meters=nearby_business_radius_meters,
                    limit=nearby_business_limit,
                )
            except Exception as e:
                logger.warning("Business nearby failed: %s", e)

        return report

    def corridor(self, path: list[tuple[float, float]],
                 buffer_meters: float, *,
                 layers: list[str] | None = None,
                 limit_per_layer: int = 5000) -> CorridorReport:
        """Query features that intersect a buffered route corridor."""
        if layers is None:
            layers = ["buildings", "business", "roads", "parks", "water"]

        report = CorridorReport()

        # Calculate bounding box around the path with buffer
        lats = [p[1] for p in path]  # path is (lat, lon) for input
        lons = [p[0] for p in path]  # but could be (lon, lat) — check
        min_lat = min(lats) - (buffer_meters / 111_320)
        max_lat = max(lats) + (buffer_meters / 111_320)
        min_lon = min(lons) - (buffer_meters / (111_320 * 0.76))
        max_lon = max(lons) + (buffer_meters / (111_320 * 0.76))

        if "buildings" in layers and self.buildings:
            try:
                report.buildings = self.buildings.get_in_bounds(
                    min_lat, min_lon, max_lat, max_lon, limit=limit_per_layer
                )
            except Exception as e:
                logger.warning("Corridor buildings failed: %s", e)

        if "business" in layers and self.business:
            try:
                report.business = self.business.get_in_bounds(
                    min_lat, min_lon, max_lat, max_lon, limit=limit_per_layer
                )
            except Exception as e:
                logger.warning("Corridor business failed: %s", e)

        if "roads" in layers and self.roads:
            try:
                report.roads = self.roads.get_in_bounds(
                    min_lat, min_lon, max_lat, max_lon, limit=limit_per_layer
                )
            except Exception as e:
                logger.warning("Corridor roads failed: %s", e)

        if "parks" in layers and self.parks:
            try:
                report.parks = self.parks.get_in_bounds(
                    min_lat, min_lon, max_lat, max_lon, limit=limit_per_layer
                )
            except Exception as e:
                logger.warning("Corridor parks failed: %s", e)

        if "water" in layers and self.water:
            try:
                report.water = self.water.get_in_bounds(
                    min_lat, min_lon, max_lat, max_lon, limit=limit_per_layer
                )
            except Exception as e:
                logger.warning("Corridor water failed: %s", e)

        return report

    def close(self) -> None:
        """Close all open readers."""
        for reader in [self.buildings, self.roads, self.water, self.admin,
                       self.places, self.rail, self.parks, self.business]:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass

    @staticmethod
    def _latlng_to_cell_int(lat: float, lon: float) -> int:
        import h3
        cell = h3.latlng_to_cell(lat, lon, 7)
        return int(cell, 16) if isinstance(cell, str) else cell
