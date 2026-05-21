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
    b"PTILESF": "buildings_v8",
    b"PTILESR": "roads",
    b"PTILESA": "admin",
    b"PTILESW": "water",
    b"PTILESP": "places",
    b"PTILEST": "rail",
    b"PTILESN": "parks",
    b"PTILESB": "business",
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
    elif args.command == "route":
        cmd_route(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
