"""
ptiles — Python client library for PTiles binary format.

Consumes the per-state `.ptiles` files at
~/kino/projects/ptiles/data/states/*.ptiles.

Provides per-layer readers (BuildingsReader, RoadsReader, WaterReader,
AdminReader, PlacesReader, RailReader, ParkReader, BusinessReader),
a composite client (PtilesClient), and a router stub (PtilesRouter).
"""

from __future__ import annotations

# --- Error hierarchy ---

class PTilesError(Exception):
    """Base error for all PTiles operations."""
    code: str = "unknown"

class MagicError(PTilesError):
    code = "magic"

class VersionError(PTilesError):
    code = "version"

class IndexError(PTilesError):
    code = "index"

class DecompressError(PTilesError):
    code = "decompress"

class ParseError(PTilesError):
    code = "parse"

class CategorySidecarError(PTilesError):
    code = "category-sidecar"

class RouterError(PTilesError):
    code = "router"

class GeoError(PTilesError):
    code = "geo"


# --- Data models ---

from ptiles.buildings import Building, BuildingsReader
from ptiles.roads import (
    RoadSegment, IntersectionType, Intersection, NearestRoad, RoadsReader,
)
from ptiles.water import WaterFeature, LargeWaterBody, GeomType, WaterReader
from ptiles.admin import AdminInfo, AdminPolygon, AdminReader
from ptiles.places import Place, PlacesReader
from ptiles.rail import RailFeature, RailReader
from ptiles.parks import ParkFeature, ParkReader
from ptiles.business import Business, BusinessHit, OperatingStatus, BusinessReader
from ptiles.composite import PtilesClient, PointReport, CorridorReport
from ptiles.router import PtilesRouter, Route

# --- Re-exports for convenience ---

__all__ = [
    # Errors
    "PTilesError", "MagicError", "VersionError", "IndexError",
    "DecompressError", "ParseError", "CategorySidecarError",
    "RouterError", "GeoError",

    # Data models
    "Building", "BuildingsReader",
    "RoadSegment", "IntersectionType", "Intersection", "NearestRoad", "RoadsReader",
    "WaterFeature", "LargeWaterBody", "GeomType", "WaterReader",
    "AdminInfo", "AdminPolygon", "AdminReader",
    "Place", "PlacesReader",
    "RailFeature", "RailReader",
    "ParkFeature", "ParkReader",
    "Business", "BusinessHit", "OperatingStatus", "BusinessReader",

    # Composite
    "PtilesClient", "PointReport", "CorridorReport",

    # Router
    "PtilesRouter", "Route",
]
