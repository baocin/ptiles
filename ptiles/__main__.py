#!/usr/bin/env python3
"""
PTiles CLI — python -m ptiles

Usage:
    python -m ptiles inspect FILE                 Print header summary
    python -m ptiles query buildings FILE LAT LON Query buildings
    python -m ptiles query water FILE LAT LON     Query water features
    python -m ptiles query admin FILE LAT LON     Query admin info
    python -m ptiles nearest-road FILE LAT LON    Find nearest road
    python -m ptiles nearby business FILE LAT LON --radius 500 --limit 5
    python -m ptiles route ROADS A_LAT A_LON B_LAT B_LON

    python -m ptiles nearest business FILE LAT LON --name "Taco Bell"
    python -m ptiles nearest water    FILE LAT LON --type river,stream
    python -m ptiles seen-by US.camera.ptiles LAT LON [--buildings ST.buildings_v9.ptiles]
    python -m ptiles sun     US.admin.ptiles  LAT LON [--at ISO8601] [--buildings ...]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import zstandard as zstd

from ptiles.codec import read_header, read_index, HEADER_SIZE
from ptiles.buildings import BuildingsReader
from ptiles.roads import RoadsReader
from ptiles.water import WaterReader
from ptiles.admin import AdminReader
from ptiles.places import PlacesReader
from ptiles.rail import RailReader
from ptiles.parks import ParkReader
from ptiles.business import BusinessReader

logger = logging.getLogger("ptiles.cli")

MAGIC_TO_LAYER = {
    b"PTILESF": "buildings",
    b"PTILESR": "roads",
    # Address files built before the PTILESD fix also carry PTILESA, so this
    # name is ambiguous for anything from v4-20260711 or earlier.
    b"PTILESA": "admin (or a pre-fix address file)",
    b"PTILESD": "address",
    b"PTILESW": "water",
    b"PTILESP": "places",
    b"PTILEST": "rail",
    b"PTILESN": "parks",
    b"PTILESB": "business",
    b"PTILESX": "business_name_index",
    b"PTILESC": "camera",
    b"PTILESS": "signals",
}


def cmd_inspect(args: argparse.Namespace) -> None:
    """Print header summary of a PTiles file."""
    filepath = args.file
    with open(filepath, "rb") as f:
        header = read_header(f)
        magic = header["magic"]
        layer = MAGIC_TO_LAYER.get(magic[:7], "unknown")

        print(f"File:      {filepath}")
        print(f"Layer:     {layer}")
        print(f"Magic:     {magic!r}")
        print(f"Version:   {header['version']}")
        print(f"Bounds:    ({header['min_lat']:.6f}, {header['min_lon']:.6f}) "
              f"to ({header['max_lat']:.6f}, {header['max_lon']:.6f})")
        print(f"Features:  {header['feature_count']:,}")
        print(f"Blocks:    {header['block_count']:,}")
        print(f"Dict:      offset={header['dict_offset']}, len={header['dict_length']}")
        print(f"Index:     offset={header['index_offset']}, len={header['index_length']}")
        print(f"Blocks at: offset={header['blocks_offset']}")
        print(f"Aux:       offset={header['aux_offset']}, len={header['aux_length']}")

        # Also show first few index entries
        try:
            f.seek(header["index_offset"])
            idx_bytes = f.read(header["index_length"])
            idx_entries = read_index(idx_bytes)
            print(f"\nIndex entries: {len(idx_entries)} total")
            for i, e in enumerate(idx_entries[:3]):
                print(f"  [{i}] h3_cell=0x{e['h3_cell']:016x} "
                      f"offset={e['block_offset']} len={e['block_length']} "
                      f"count={e['feature_count']}")
            if len(idx_entries) > 3:
                print(f"  ... and {len(idx_entries) - 3} more")
        except Exception as e:
            pass


def cmd_query_buildings(args: argparse.Namespace) -> None:
    reader = BuildingsReader.open(args.file)
    try:
        result = reader.query(args.lat, args.lon)
        if result:
            print(json.dumps({
                "osm_id": result.osm_id,
                "building_type": result.building_type,
                "centroid_lat": result.centroid_lat,
                "centroid_lon": result.centroid_lon,
                "name": result.name,
                "category": result.category,
                "vertex_count": len(result.coordinates),
            }, indent=2))
        else:
            print("No building found")
    finally:
        reader.close()


def cmd_nearest_road(args: argparse.Namespace) -> None:
    reader = RoadsReader.open(args.file)
    try:
        result = reader.nearest(args.lat, args.lon,
                                radius_meters=getattr(args, 'radius', 100))
        if result:
            print(json.dumps({
                "osm_id": result.road.osm_id,
                "road_class": result.road.road_class,
                "name": result.road.name,
                "ref_tag": result.road.ref_tag,
                "distance_meters": result.distance_meters,
                "snapped_lat": result.snapped_lat,
                "snapped_lon": result.snapped_lon,
            }, indent=2))
        else:
            print("No road found within radius")
    finally:
        reader.close()


def cmd_nearby_business(args: argparse.Namespace) -> None:
    reader = BusinessReader.open(args.file)
    try:
        results = reader.nearby(
            args.lat, args.lon,
            radius_meters=args.radius,
            limit=args.limit,
            category_prefix=getattr(args, 'category_prefix', None),
        )
        if results:
            output = []
            for hit in results:
                b = hit.business
                output.append({
                    "distance_meters": hit.distance_meters,
                    "name": b.name,
                    "category": b.category,
                    "address": b.address,
                    "phone": b.phone,
                    "operating_status": b.operating_status,
                })
            print(json.dumps(output, indent=2))
        else:
            print("No businesses found")
    finally:
        reader.close()


def cmd_query_water(args: argparse.Namespace) -> None:
    reader = WaterReader.open(args.file)
    try:
        results = reader.get_in_cell(
            __import__("h3").latlng_to_cell(args.lat, args.lon, 7)
        )
        if results:
            output = []
            for feat in results[:20]:
                output.append({
                    "osm_id": feat.osm_id,
                    "water_type": feat.water_type,
                    "name": feat.name,
                    "geom_type": feat.geom_type.name,
                    "vertex_count": len(feat.coords),
                })
            print(json.dumps(output, indent=2))
        else:
            print("No water features found")
    finally:
        reader.close()


def cmd_query_admin(args: argparse.Namespace) -> None:
    reader = AdminReader.open(args.file)
    try:
        result = reader.query(args.lat, args.lon)
        if result:
            print(json.dumps({
                "country": result.country,
                "state": result.state,
                "county": result.county,
                "zip": result.zip,
                "timezone": result.timezone,
            }, indent=2))
        else:
            print("No admin data found (ocean or outside coverage)")
    finally:
        reader.close()


NEAREST_READERS = {
    "business": ("ptiles.business", "BusinessReader"),
    "camera": ("ptiles.camera", "CameraReader"),
    "signals": ("ptiles.signals", "SignalsReader"),
    "water": ("ptiles.water", "WaterReader"),
    "parks": ("ptiles.parks", "ParkReader"),
    "rail": ("ptiles.rail", "RailReader"),
    "places": ("ptiles.places", "PlacesReader"),
    "buildings": ("ptiles.buildings", "BuildingsReader"),
}


def _representative_point(f) -> tuple[float | None, float | None]:
    """A single lat/lon for a feature, whatever its geometry.

    Point layers carry scalar lat/lon; line and area layers only have a
    coordinate list, so report its midpoint rather than nothing.
    """
    lat = getattr(f, "lat", None)
    lon = getattr(f, "lon", None)
    if lat is not None and lon is not None:
        return lat, lon
    lat = getattr(f, "centroid_lat", None)
    lon = getattr(f, "centroid_lon", None)
    if lat is not None and lon is not None:
        return round(lat, 5), round(lon, 5)
    coords = getattr(f, "coordinates", None) or getattr(f, "coords", None)
    if coords:
        mid = coords[len(coords) // 2]
        return round(mid[1], 5), round(mid[0], 5)
    return None, None


def cmd_nearest(args: argparse.Namespace) -> None:
    """Spiral outward from a point until the nearest matches are settled."""
    import importlib
    from ptiles.nearest import match_attr, match_name, nearest

    module_name, cls_name = NEAREST_READERS[args.layer]
    reader_cls = getattr(importlib.import_module(module_name), cls_name)
    reader = reader_cls.open(args.file)
    try:
        predicate = None
        if args.name:
            predicate = match_name(args.name)
        elif args.type:
            attr = {"water": "water_type", "signals": "signal_type",
                    "camera": "device_type", "places": "place_type",
                    "rail": "rail_type", "buildings": "building_type",
                    }.get(args.layer, "category")
            predicate = match_attr(attr, *args.type.split(","))

        result = nearest(reader, args.lat, args.lon, predicate=predicate,
                         n=args.limit, max_meters=args.radius)

        out = []
        for hit in result:
            f = hit.feature
            lat, lon = _representative_point(f)
            out.append({
                "distance_meters": round(hit.distance_meters, 1),
                "name": getattr(f, "name", None),
                "type": (getattr(f, "water_type", None)
                         or getattr(f, "signal_type", None)
                         or getattr(f, "device_type", None)
                         or getattr(f, "category", None)
                         or getattr(f, "building_type", None)),
                "osm_id": getattr(f, "osm_id", None),
                "lat": lat,
                "lon": lon,
            })
        print(json.dumps({
            "hits": out,
            "rings_scanned": result.rings_scanned,
            "cells_read": result.cells_read,
            "features_examined": result.features_examined,
            "exhausted": result.exhausted,
        }, indent=2))
    finally:
        reader.close()


def cmd_seen_by(args: argparse.Namespace) -> None:
    """Which cameras are aimed at this point."""
    from ptiles.camera import CameraReader
    from ptiles.visibility import cameras_seeing

    reader = CameraReader.open(args.file)
    buildings = None
    try:
        if args.buildings:
            from ptiles.buildings import BuildingsReader
            buildings = BuildingsReader.open(args.buildings)

        sightings = cameras_seeing(
            args.lat, args.lon, reader,
            buildings=buildings,
            max_meters=args.radius,
            include_occluded=args.include_occluded,
        )
        print(json.dumps({
            "cameras_seeing": [
                {
                    "osm_id": s.camera.osm_id,
                    "device_type": s.camera.device_type,
                    "camera_type": s.camera.camera_type,
                    "operator": s.camera.operator,
                    "distance_meters": round(s.distance_meters, 1),
                    "bearing_from_camera": round(s.bearing_from_camera, 1),
                    "off_axis_degrees": (round(s.off_axis_degrees, 1)
                                         if s.off_axis_degrees is not None else None),
                    "confidence": s.confidence.value,
                    "assumed_range_m": s.assumed_range_m,
                    "assumed_fov_deg": s.assumed_fov_deg,
                    "occluded_by": s.occluded_by,
                }
                for s in sightings
            ],
            # Stated so a caller never mistakes a defaulted cone for a
            # measured one.
            "note": ("range is always assumed (not in the format); field of "
                     "view is assumed unless confidence is 'certain'"),
        }, indent=2))
    finally:
        reader.close()
        if buildings is not None:
            buildings.close()


def cmd_sun(args: argparse.Namespace) -> None:
    """Sun position, local time, and optionally whether a point is shaded."""
    from datetime import datetime, timezone
    from ptiles.admin import AdminReader
    from ptiles.sun import is_shaded, local_time, sun_position

    when = (datetime.fromisoformat(args.at) if args.at
            else datetime.now(timezone.utc))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    admin = AdminReader.open(args.file)
    out: dict = {}
    try:
        info = admin.query(args.lat, args.lon)
        loc = local_time(args.lat, args.lon, admin, when)
        pos = sun_position(args.lat, args.lon, when)
        out = {
            "timezone": info.timezone if info else None,
            "local_time": loc.isoformat(),
            "utc": when.astimezone(timezone.utc).isoformat(),
            "azimuth": round(pos.azimuth, 2),
            "altitude": round(pos.altitude, 2),
            "sun_is_up": pos.is_up,
        }
        if args.buildings:
            from ptiles.buildings import BuildingsReader
            b = BuildingsReader.open(args.buildings)
            try:
                shade = is_shaded(args.lat, args.lon, b, when)
                out["shade"] = {
                    # None means unknown, and is not the same as sunlit.
                    "shaded": shade.shaded,
                    "shaded_by": shade.shaded_by,
                    "buildings_considered": shade.buildings_considered,
                    "buildings_with_height": shade.buildings_with_height,
                    "reason": shade.reason,
                }
            finally:
                b.close()
    finally:
        admin.close()
    print(json.dumps(out, indent=2))


def cmd_route(args: argparse.Namespace) -> None:
    from ptiles.router import PtilesRouter
    router = PtilesRouter.open(args.file)
    result = router.route(args.src_lat, args.src_lon,
                          args.dst_lat, args.dst_lon,
                          profile=args.profile)
    print(json.dumps({
        "distance_meters": result.distance_meters,
        "duration_seconds": result.duration_seconds,
        "from_cell": result.from_cell,
        "to_cell": result.to_cell,
        "segments": result.segments,
        "profile": result.profile,
    }, indent=2))


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="PTiles CLI")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # inspect
    p_inspect = subparsers.add_parser("inspect", help="Print header summary")
    p_inspect.add_argument("file", help="Path to .ptiles file")

    # query buildings
    p_qb = subparsers.add_parser("query", help="Query a layer")
    p_qb.add_argument("layer", choices=["buildings", "water", "admin"],
                      help="Layer to query")
    p_qb.add_argument("file", help="Path to .ptiles file")
    p_qb.add_argument("lat", type=float)
    p_qb.add_argument("lon", type=float)

    # nearest-road
    p_nr = subparsers.add_parser("nearest-road", help="Find nearest road")
    p_nr.add_argument("file", help="Path to .roads.ptiles file")
    p_nr.add_argument("lat", type=float)
    p_nr.add_argument("lon", type=float)
    p_nr.add_argument("--radius", type=float, default=100,
                      help="Search radius in meters")

    # nearby
    p_nb = subparsers.add_parser("nearby", help="Nearby query")
    p_nb.add_argument("layer", choices=["business"], help="Layer to query")
    p_nb.add_argument("file", help="Path to .business.ptiles file")
    p_nb.add_argument("lat", type=float)
    p_nb.add_argument("lon", type=float)
    p_nb.add_argument("--radius", type=float, default=1000,
                      help="Search radius in meters")
    p_nb.add_argument("--limit", type=int, default=10,
                      help="Max results")
    p_nb.add_argument("--category-prefix", help="Filter by category prefix")

    # nearest
    p_ne = subparsers.add_parser(
        "nearest", help="Nearest feature by name or type (spirals outward)")
    p_ne.add_argument("layer", choices=sorted(NEAREST_READERS))
    p_ne.add_argument("file", help="Path to the layer's .ptiles file")
    p_ne.add_argument("lat", type=float)
    p_ne.add_argument("lon", type=float)
    p_ne.add_argument("--name", help='Match name or brand, e.g. "Taco Bell"')
    p_ne.add_argument("--type", help="Match type, comma-separated, e.g. river,stream")
    p_ne.add_argument("--limit", type=int, default=1, help="How many to return")
    p_ne.add_argument("--radius", type=float, default=None,
                      help="Give up beyond this many meters")

    # seen-by
    p_sb = subparsers.add_parser(
        "seen-by", help="Which cameras are aimed at this point")
    p_sb.add_argument("file", help="Path to US.camera.ptiles")
    p_sb.add_argument("lat", type=float)
    p_sb.add_argument("lon", type=float)
    p_sb.add_argument("--radius", type=float, default=None,
                      help="Cap distance; default is each device's assumed range")
    p_sb.add_argument("--buildings", help="Buildings file, to test occlusion")
    p_sb.add_argument("--include-occluded", action="store_true",
                      help="Keep blocked sightlines, flagged rather than dropped")

    # sun
    p_su = subparsers.add_parser(
        "sun", help="Sun position and local time; shade with --buildings")
    p_su.add_argument("file", help="Path to US.admin.ptiles")
    p_su.add_argument("lat", type=float)
    p_su.add_argument("lon", type=float)
    p_su.add_argument("--at", help="ISO 8601 instant; default now")
    p_su.add_argument("--buildings", help="Buildings file, to test shade")

    # route
    p_rt = subparsers.add_parser("route", help="Compute a route")
    p_rt.add_argument("file", help="Path to .roads.ptiles file")
    p_rt.add_argument("src_lat", type=float)
    p_rt.add_argument("src_lon", type=float)
    p_rt.add_argument("dst_lat", type=float)
    p_rt.add_argument("dst_lon", type=float)
    p_rt.add_argument("--profile", default="driving",
                      choices=["driving", "walking", "cycling"])

    args = parser.parse_args()

    if args.command == "inspect":
        cmd_inspect(args)
    elif args.command == "query":
        if args.layer == "buildings":
            cmd_query_buildings(args)
        elif args.layer == "water":
            cmd_query_water(args)
        elif args.layer == "admin":
            cmd_query_admin(args)
        else:
            print(f"Unknown query layer: {args.layer}")
    elif args.command == "nearest-road":
        cmd_nearest_road(args)
    elif args.command == "nearby":
        if args.layer == "business":
            cmd_nearby_business(args)
        else:
            print(f"Unknown nearby layer: {args.layer}")
    elif args.command == "nearest":
        cmd_nearest(args)
    elif args.command == "seen-by":
        cmd_seen_by(args)
    elif args.command == "sun":
        cmd_sun(args)
    elif args.command == "route":
        cmd_route(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
