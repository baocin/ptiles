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
from ptiles.trails import Trail, TrailsReader
from ptiles.ev import Charger, EvReader
from ptiles.water import WaterFeature, WaterReader

logger = logging.getLogger("ptiles.composite")

# Point queries answer with the *nearest* feature, which can sit outside the
# file's own bbox when the point is near an edge, so the bounds filter is padded
# rather than exact. ~0.05 deg is about 5.5km -- wider than any nearest-feature
# search radius the layers use.
NEIGHBOUR_PAD_DEG = 0.05


def lon_within(west: float, east: float, lon: float, pad: float = 0.0) -> bool:
    """Whether `lon` falls in the range west..east, going eastward.

    A box that crosses the antimeridian has west > east (Fiji is 177E..-179E).
    Comparing `west <= lon <= east` on such a box is false for every longitude
    on Earth, so the layer is skipped and the query returns nothing -- silently,
    since an empty answer looks exactly like "no features here". Countries this
    affects: Fiji, New Zealand's Chathams, Russia, Kiribati, and Alaska, whose
    row in states.py sidesteps it today by claiming the whole globe.
    """
    if west <= east:
        return west - pad <= lon <= east + pad
    return lon >= west - pad or lon <= east + pad


def lon_ranges_overlap(west: float, east: float, min_lon: float, max_lon: float) -> bool:
    """Whether two longitude ranges overlap, either of which may wrap."""
    a_wraps, b_wraps = west > east, min_lon > max_lon
    if not a_wraps and not b_wraps:
        return not (max_lon < west or min_lon > east)
    if a_wraps and b_wraps:
        return True  # both include the antimeridian, so they share it
    if a_wraps:
        return min_lon <= east or max_lon >= west
    return west <= max_lon or east >= min_lon


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
    trails: list[Trail] = field(default_factory=list)
    chargers: list[Charger] = field(default_factory=list)
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
    trails: list[Trail] = field(default_factory=list)
    chargers: list[Charger] = field(default_factory=list)


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


class _LayerBase:
    """Shared bounds test and cleanup for the adapters.

    One layer can be backed by several files. Japan's buildings are eight
    regional files because 29.5M buildings do not fit a single build, while its
    other layers are country-wide -- so a client holding "Japan" holds eight
    BuildingLayer instances and one PlaceLayer.

    That makes bounds a correctness concern, not just a speed one. Adapters
    write their answers into a shared report, so a file that does not contain
    the query point must be skipped rather than allowed to write its empty
    answer over a real one.
    """

    _reader = None
    _scope = None

    @property
    def scope(self):
        """Which scope this file covers, e.g. 'TN' or 'JP-KANTO'."""
        return self._scope

    @property
    def bounds(self):
        """(min_lat, min_lon, max_lat, max_lon), or None when unknown.

        PTLR roads files carry no bbox, so they answer None and are always
        consulted.
        """
        try:
            h = self._reader.header
            return (h["min_lat"], h["min_lon"], h["max_lat"], h["max_lon"])
        except Exception:
            return None

    def covers(self, lat, lon, pad=0.0):
        b = self.bounds
        if b is None:
            return True
        s, w, n, e = b
        return (s - pad) <= lat <= (n + pad) and lon_within(w, e, lon, pad)

    def intersects(self, min_lat, min_lon, max_lat, max_lon):
        b = self.bounds
        if b is None:
            return True
        s, w, n, e = b
        if max_lat < s or min_lat > n:
            return False
        return lon_ranges_overlap(w, e, min_lon, max_lon)

    def close(self):
        self._reader.close()


class BuildingLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = BuildingsReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            hit = self._reader.query(lat, lon)
            # Only a hit may overwrite. Region extracts overlap at their seams,
            # so two files can legitimately answer for one point and the misses
            # must not erase the hit.
            if hit is not None:
                report.building = hit
        except Exception as e:
            logger.warning("Buildings query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.buildings.extend(
                self._reader.get_in_bounds(
                    min_lat, min_lon, max_lat, max_lon, limit=limit
                )
            )
        except Exception as e:
            logger.warning("Corridor buildings failed: %s", e)


class AdminLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = AdminReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            hit = self._reader.query(lat, lon)
            if hit is not None:
                report.admin = hit
        except Exception as e:
            logger.warning("Admin query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        """No-op: admin is a per-cell lookup grid, not a feature list.

        Present so `corridor()` can call every layer uniformly.
        """

    def close(self):
        self._reader.close()


class RoadLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = RoadsReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            near = self._reader.nearest(lat, lon)
            # Keep the closest across files, not the last file's answer.
            if near is not None and (
                report.nearest_road is None
                or getattr(near, 'distance_meters', float('inf'))
                < getattr(report.nearest_road, 'distance_meters', float('inf'))
            ):
                report.nearest_road = near
            report.nearby_roads.extend(self._reader.nearest_n(lat, lon, n=5))
        except Exception as e:
            logger.warning("Roads nearest query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.roads.extend(self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            ))
        except Exception as e:
            logger.warning("Corridor roads failed: %s", e)

    def close(self):
        self._reader.close()


class WaterLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = WaterReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            import h3

            cell = int(h3.latlng_to_cell(lat, lon, 7), 16)
            report.water.extend(self._reader.get_in_cell(cell))
        except Exception as e:
            logger.warning("Water query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.water.extend(self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            ))
        except Exception as e:
            logger.warning("Corridor water failed: %s", e)

    def close(self):
        self._reader.close()


class ParkLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = ParkReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            import h3

            cell = int(h3.latlng_to_cell(lat, lon, 7), 16)
            report.parks.extend(self._reader.get_in_cell(cell))
        except Exception as e:
            logger.warning("Parks query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.parks.extend(self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            ))
        except Exception as e:
            logger.warning("Corridor parks failed: %s", e)

    def close(self):
        self._reader.close()


class RailLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = RailReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            import h3

            cell = int(h3.latlng_to_cell(lat, lon, 7), 16)
            report.rail.extend(self._reader.get_in_cell(cell))
        except Exception as e:
            logger.warning("Rail query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.rail.extend(self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            ))
        except Exception as e:
            logger.warning("Corridor rail failed: %s", e)

    def close(self):
        self._reader.close()


class PlaceLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = PlacesReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            report.places.extend(self._reader.get_in_bounds(
                lat - 0.05, lon - 0.05, lat + 0.05, lon + 0.05, limit=20
            ))
        except Exception as e:
            logger.warning("Places query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.places.extend(self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            ))
        except Exception as e:
            logger.warning("Corridor places failed: %s", e)

    def close(self):
        self._reader.close()


class CameraLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = CameraReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            from ptiles.visibility import cameras_near, cameras_seeing

            radius = kw.get("camera_radius_meters", 200)
            report.cameras.extend(s.camera for s in
                                  cameras_near(lat, lon, self._reader, max_meters=radius))
            report.cameras_seeing.extend(cameras_seeing(
                lat, lon, self._reader,
                buildings=kw.get("buildings_reader"),
            ))
        except Exception as e:
            logger.warning("Camera query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.cameras.extend(self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            ))
        except Exception as e:
            logger.warning("Corridor camera failed: %s", e)

    def close(self):
        self._reader.close()


class SignalLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = SignalsReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            from ptiles.nearest import nearest

            found = nearest(self._reader, lat, lon, n=kw.get("signal_limit", 10),
                            max_meters=kw.get("signal_radius_meters", 250))
            report.signals.extend(h.feature for h in found)
        except Exception as e:
            logger.warning("Signals query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.signals.extend(self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            ))
        except Exception as e:
            logger.warning("Corridor signals failed: %s", e)

    def close(self):
        self._reader.close()


class TrailLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = TrailsReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            import h3

            cell = int(h3.latlng_to_cell(lat, lon, 7), 16)
            report.trails.extend(self._reader.get_in_cell(cell))
        except Exception as e:
            logger.warning("Trails query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.trails.extend(self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            ))
        except Exception as e:
            logger.warning("Corridor trails failed: %s", e)


class EvLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = EvReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            # Chargers are sparse, so a cell lookup usually returns nothing --
            # widen to the surrounding degree-ish box the way places does.
            report.chargers.extend(self._reader.get_in_bounds(
                lat - 0.05, lon - 0.05, lat + 0.05, lon + 0.05, limit=20
            ))
        except Exception as e:
            logger.warning("EV query failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.chargers.extend(self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            ))
        except Exception as e:
            logger.warning("Corridor EV failed: %s", e)


class BusinessLayer(_LayerBase):
    def __init__(self, path, scope=None):
        self._reader = BusinessReader.open(path)
        self._scope = scope

    def query_point(self, lat, lon, report, **kw):
        try:
            report.businesses.extend(self._reader.nearby(
                lat,
                lon,
                radius_meters=kw.get("business_radius_meters", 500),
                limit=kw.get("business_limit", 5),
            ))
        except Exception as e:
            logger.warning("Business nearby failed: %s", e)

    def query_corridor(self, min_lat, min_lon, max_lat, max_lon, report, limit):
        try:
            report.business.extend(self._reader.get_in_bounds(
                min_lat, min_lon, max_lat, max_lon, limit=limit
            ))
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
    ("trails", TrailLayer, True, True),
    ("ev", EvLayer, True, True),
]

# Layers published as one file for a whole country rather than per subdivision,
# so they are looked up under the country code -- US.admin.ptiles for a US
# scope, JP.signals.ptiles for a Japanese one. This used to hardcode the string
# "US.", which made every non-US country's signals and cameras unreachable.
COUNTRY_WIDE_LAYERS: dict[str, type] = {
    "admin": AdminLayer,
    "camera": CameraLayer,
    "signals": SignalLayer,
}

# Kept as an alias: this was the public name before non-US countries existed.
US_WIDE_LAYERS = COUNTRY_WIDE_LAYERS

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
    "trails": ("trails_v1", "trails"),
    "ev": ("ev_v1", "ev"),
}


def _manifest_relpath(scope, layer, entry, meta) -> str | None:
    """Where a manifest says a file lives, relative to the snapshot root.

    Prefers the recorded `path`. Manifests published before countries existed
    have no such field, so fall back to the layer's filename `pattern` (or the
    layer/version) and apply the standard layout rule.
    """
    from ptiles.scopes import publish_relpath

    meta = meta or {}
    rel = meta.get("path")
    if rel:
        return rel
    pattern = entry.get("pattern")
    # The scope's own version wins over the layer's: once countries can sit at
    # different versions the layer-level one is null, and only the per-scope
    # value says what this file is actually called.
    version = meta.get("version", entry.get("version"))
    if version:
        filename = f"{scope}.{layer}_v{version}.ptiles"
    elif pattern:
        filename = pattern.replace("{scope}", scope)
    else:
        filename = f"{scope}.{layer}.ptiles"
    try:
        return publish_relpath(scope, filename)
    except ValueError:
        return None


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

    @staticmethod
    def _find(data_dir: Path, country: str, filename: str) -> Path | None:
        """Locate a file in a local mirror of a published snapshot.

        Published snapshots keep the US at the root and give every other country
        its own directory, so try both rather than making callers care.
        """
        for candidate in (data_dir / filename, data_dir / country / filename):
            if candidate.exists():
                return candidate
        return None

    @classmethod
    def open_scope(cls, scope: str, data_dir: str | os.PathLike) -> "PtilesClient":
        """Open every layer available for one scope, e.g. 'TN' or 'JP-KANTO'."""
        client = cls()
        client._load_scope(Path(data_dir), scope.upper())
        client._wire_cross_layer()
        return client

    @classmethod
    def open_state(cls, state: str, data_dir: str | os.PathLike) -> "PtilesClient":
        """Open all available <SCOPE>.<layer>.ptiles files in data_dir.

        Retained under its original name; `open_scope` is the same thing without
        the US-state framing.
        """
        return cls.open_scope(state, data_dir)

    @classmethod
    def open_country(
        cls, country: str, data_dir: str | os.PathLike, scopes: list[str] | None = None
    ) -> "PtilesClient":
        """Open every scope belonging to one country.

        Granularity varies by layer and by country: Japan's buildings are eight
        regional files while its places are one country-wide file, so this can
        return a client holding several files for one layer. Queries pick among
        them by bounds.

        A `manifest.json` beside the data is authoritative and is used when
        present -- it names the exact layer versions, so nothing is guessed. It
        is optional: without one the directory is scanned and filenames are
        probed instead, which is what a partial or hand-assembled download
        looks like.

        `scopes` overrides discovery; otherwise the directory is scanned.
        """
        data_dir = Path(data_dir)
        country = country.upper()

        manifest = data_dir / "manifest.json"
        if manifest.exists() and scopes is None:
            return cls.from_manifest(manifest, data_dir, country=country)

        client = cls()
        found = scopes if scopes is not None else cls.discover_scopes(data_dir, country)
        for scope in sorted(found):
            client._load_scope(data_dir, scope)
        client._wire_cross_layer()
        return client

    @staticmethod
    def discover_scopes(data_dir: str | os.PathLike, country: str) -> list[str]:
        """Scopes of one country present in a local snapshot directory."""
        from ptiles.scopes import country_of, scope_of_filename

        data_dir = Path(data_dir)
        country = country.upper()
        seen = set()
        # Root holds the US set; other countries live in a country directory.
        for d in (data_dir, data_dir / country):
            if not d.is_dir():
                continue
            for f in d.glob("*.ptiles"):
                scope = scope_of_filename(f.name)
                try:
                    if country_of(scope) == country:
                        seen.add(scope)
                except ValueError:
                    continue
        return sorted(seen)

    @classmethod
    def from_manifest(
        cls,
        manifest: dict | str | os.PathLike,
        data_dir: str | os.PathLike,
        *,
        country: str | None = None,
        scopes: list[str] | None = None,
    ) -> "PtilesClient":
        """Open the layers a published manifest declares.

        The manifest states which layer versions exist and where each file sits,
        so nothing here has to guess filenames the way LAYER_FILE_ALIASES does.

        Tolerates manifests published before countries existed: those carry no
        `countries` index and no per-scope `path`, both of which are derived
        from the scope names when absent. A manifest that is present but
        unreadable raises rather than falling back, because a corrupt manifest
        is a real problem and silently probing the directory would hide it.
        """
        import json

        if not isinstance(manifest, dict):
            src = Path(manifest)
            if not src.exists():
                raise FileNotFoundError(f"no manifest at {src}")
            try:
                manifest = json.loads(src.read_text())
            except json.JSONDecodeError as e:
                raise ValueError(f"manifest at {src} is not valid JSON: {e}") from e
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")

        data_dir = Path(data_dir)
        wanted = set(scopes) if scopes else None
        if wanted is None and country:
            country = country.upper()
            declared = manifest.get("countries")
            if declared:
                wanted = set(declared.get(country, []))
            else:
                # Pre-countries manifest: work it out from the scope names.
                from ptiles.scopes import country_of

                wanted = set()
                for entry in manifest.get("layers", {}).values():
                    for scope in entry.get("scopes", {}):
                        try:
                            if country_of(scope) == country:
                                wanted.add(scope)
                        except ValueError:
                            continue

        by_suffix = {suffix: factory for suffix, factory, *_ in LAYER_CONFIG}
        by_suffix.update(COUNTRY_WIDE_LAYERS)

        client = cls()
        for layer, entry in sorted(manifest.get("layers", {}).items()):
            version = entry.get("version")
            factory = by_suffix.get(f"{layer}_v{version}" if version else layer)
            if factory is None:
                factory = by_suffix.get(layer)
            if factory is None:
                # buildings ships as buildings_v9 but is keyed buildings_v8.
                factory = next(
                    (f for s, f in by_suffix.items() if s.split("_v")[0] == layer), None
                )
            if factory is None:
                logger.debug("manifest layer %s has no reader, skipping", layer)
                continue
            for scope, meta in sorted(entry.get("scopes", {}).items()):
                if wanted is not None and scope not in wanted:
                    continue
                rel = _manifest_relpath(scope, layer, entry, meta)
                if rel is None:
                    logger.warning("manifest entry for %s/%s has no usable path",
                                   layer, scope)
                    continue
                # Try the declared path, then the bare filename, so a manifest
                # also works against a flat directory of downloaded files.
                p = next(
                    (c for c in (data_dir / rel, data_dir / Path(rel).name)
                     if c.exists()),
                    None,
                )
                if p is None:
                    logger.warning("manifest lists %s but it is missing", rel)
                    continue
                try:
                    client._layers.append(factory(p, scope))
                except Exception as e:
                    logger.warning("could not open %s: %s", p, e)
        client._wire_cross_layer()
        return client

    def _load_scope(self, data_dir: Path, scope: str) -> None:
        """Append every layer file found for one scope."""
        from ptiles.scopes import country_of

        try:
            country = country_of(scope)
        except ValueError:
            logger.warning("not a scope: %r", scope)
            return

        for suffix, factory, *_ in LAYER_CONFIG:
            if suffix in COUNTRY_WIDE_LAYERS:
                continue
            for alias in LAYER_FILE_ALIASES.get(suffix, (suffix,)):
                p = self._find(data_dir, country, f"{scope}.{alias}.ptiles")
                if p is not None:
                    try:
                        self._layers.append(factory(p, scope))
                    except Exception as e:
                        logger.warning("could not open %s: %s", p, e)
                    break

        # One file for the whole country, so load it once however many scopes
        # of that country are opened.
        for suffix, factory in COUNTRY_WIDE_LAYERS.items():
            if any(
                isinstance(l, factory) and getattr(l, "scope", None) == country
                for l in self._layers
            ):
                continue
            for alias in LAYER_FILE_ALIASES.get(suffix, (suffix,)):
                p = self._find(data_dir, country, f"{country}.{alias}.ptiles")
                if p is not None:
                    try:
                        self._layers.append(factory(p, country))
                    except Exception as e:
                        logger.warning("could not open %s: %s", p, e)
                    break

    def _wire_cross_layer(self) -> None:
        """Note the layers that other layers need.

        Occlusion needs footprints and shading needs both footprints and a
        timezone, so those two answers cannot come from a single layer in
        isolation the way the rest can.

        These are lists because a country can be split across files. Picking the
        first building layer would hand occlusion and shade a reader for the
        wrong region entirely; `_reader_for` resolves by bounds at query time.
        """
        self._building_layers = [l for l in self._layers if isinstance(l, BuildingLayer)]
        self._admin_layers = [l for l in self._layers if isinstance(l, AdminLayer)]
        # Back-compat: single-file callers still read these attributes.
        self._buildings = (
            self._building_layers[0]._reader if self._building_layers else None
        )
        self._admin = self._admin_layers[0]._reader if self._admin_layers else None

    @staticmethod
    def _reader_for(layers, lat, lon):
        """The reader among `layers` whose file covers this point."""
        for layer in layers:
            if layer.covers(lat, lon):
                return layer._reader
        return None

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
        buildings_here = self._reader_for(self._building_layers, lat, lon)
        admin_here = self._reader_for(self._admin_layers, lat, lon)
        kw = dict(
            business_limit=nearby_business_limit,
            business_radius_meters=nearby_business_radius_meters,
            water_radius_meters=water_radius_meters,
            camera_radius_meters=camera_radius_meters,
            buildings_reader=buildings_here if check_occlusion else None,
        )
        for layer in self._layers:
            # Skip files that cannot contain the point. With one file per layer
            # this only saves a read; with several it is what stops a region
            # that misses from writing its empty answer over one that hit.
            # Padded, because a nearest-feature query legitimately reaches
            # outside the file's own bbox.
            if not layer.covers(lat, lon, pad=NEIGHBOUR_PAD_DEG):
                continue
            layer.query_point(lat, lon, report, **kw)

        if include_sun and buildings_here is not None:
            try:
                from datetime import datetime, timezone
                from ptiles.sun import is_shaded, local_time, sun_position

                when = at or datetime.now(timezone.utc)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if admin_here is not None:
                    # Resolve local time so a caller printing the result sees
                    # the wall clock at the location, not UTC.
                    report.local_time = local_time(lat, lon, admin_here, when)
                report.sun = sun_position(lat, lon, when)
                report.shade = is_shaded(lat, lon, buildings_here, when)
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
            # A corridor can span two regions, so this is a filter, not a
            # choice: every file that overlaps contributes, and the adapters
            # extend the report rather than replacing it.
            if not layer.intersects(min_lat, min_lon, max_lat, max_lon):
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
