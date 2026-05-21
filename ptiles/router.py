"""
Router for PTiles — PtilesRouter stub.

Wraps the Rust routing CLI or provides a basic Dijkstra implementation
over the road graph. Currently a stub that will shell out to the Rust
`ptiles route` binary when available.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ptiles import RouterError

logger = logging.getLogger("ptiles.router")


@dataclass(frozen=True, slots=True)
class Route:
    distance_meters: float
    duration_seconds: float
    from_cell: int
    to_cell: int
    segments: int
    path: tuple[tuple[float, float], ...] = ()
    profile: str = "driving"


class PtilesRouter:
    """PTiles router — shell wrapper around the Rust CLI or basic Dijkstra.

    Currently a stub. When the Rust `ptiles` binary is available at
    ~/kino/projects/timeline/target/release/ptiles, this will shell out
    to it for routing. Otherwise raises RouterError.
    """

    def __init__(self, roads_path: str | Path | None = None,
                 highways_path: str | Path | None = None):
        self._roads_path = Path(roads_path) if roads_path else None
        self._highways_path = Path(highways_path) if highways_path else None

    @classmethod
    def open(cls, roads: str | Path) -> "PtilesRouter":
        """Open router with a roads file."""
        return cls(roads_path=Path(roads))

    def attach_highways(self, highways: str | Path) -> None:
        """Attach highways file as a routing hint."""
        self._highways_path = Path(highways)

    def route(self, src_lat: float, src_lon: float,
              dst_lat: float, dst_lon: float, *,
              profile: str = "driving") -> Route:
        """Compute a route between two points.

        Attempts to use the Rust CLI first, then falls back to a
        basic great-circle approximation.
        """
        # Try Rust CLI first
        rust_binary = Path.home() / "kino/projects/timeline/target/release/ptiles"
        if rust_binary.exists() and self._roads_path:
            try:
                return self._route_via_rust(src_lat, src_lon, dst_lat, dst_lon, profile)
            except Exception as e:
                logger.warning("Rust routing failed: %s, falling back", e)

        # Fallback: basic great-circle estimation
        return self._estimate_route(src_lat, src_lon, dst_lat, dst_lon, profile)

    def _route_via_rust(self, src_lat: float, src_lon: float,
                        dst_lat: float, dst_lon: float,
                        profile: str) -> Route:
        """Route via the Rust ptiles CLI binary."""
        import h3
        rust_binary = Path.home() / "kino/projects/timeline/target/release/ptiles"
        args = [
            str(rust_binary),
            "route",
            str(self._roads_path),
            str(src_lat), str(src_lon),
            str(dst_lat), str(dst_lon),
            "--profile", profile,
        ]
        if self._highways_path:
            args.extend(["--highways", str(self._highways_path)])

        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RouterError(f"Rust CLI failed: {result.stderr.strip()}")

        import json
        data = json.loads(result.stdout)
        path_coords = tuple((p[0], p[1]) for p in data.get("path", []))
        src_cell = h3.latlng_to_cell(src_lat, src_lon, 7)
        dst_cell = h3.latlng_to_cell(dst_lat, dst_lon, 7)
        src_cell_int = int(src_cell, 16) if isinstance(src_cell, str) else src_cell
        dst_cell_int = int(dst_cell, 16) if isinstance(dst_cell, str) else dst_cell

        return Route(
            distance_meters=data.get("distance_meters", 0.0),
            duration_seconds=data.get("duration_seconds", 0.0),
            from_cell=src_cell_int,
            to_cell=dst_cell_int,
            segments=data.get("segments", 0),
            path=path_coords,
            profile=data.get("profile", profile),
        )

    def _estimate_route(self, src_lat: float, src_lon: float,
                        dst_lat: float, dst_lon: float,
                        profile: str) -> Route:
        """Basic great-circle route estimate (no graph)."""
        import math
        import h3

        # Haversine
        R = 6_371_000.0
        phi1 = math.radians(src_lat)
        phi2 = math.radians(dst_lat)
        dphi = math.radians(dst_lat - src_lat)
        dlam = math.radians(dst_lon - src_lon)
        a = (math.sin(dphi / 2) ** 2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
        distance = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        # Rough duration estimate
        speed_map = {"driving": 15.0, "cycling": 5.0, "walking": 1.4}
        speed = speed_map.get(profile, 15.0)
        duration = distance / speed

        # Simple path: great-circle line sampled at ~100 m intervals
        n_points = max(2, int(distance / 100))
        path: list[tuple[float, float]] = []
        for i in range(n_points + 1):
            t = i / n_points
            lat = src_lat + (dst_lat - src_lat) * t
            lon = src_lon + (dst_lon - src_lon) * t
            path.append((lon, lat))

        src_cell = h3.latlng_to_cell(src_lat, src_lon, 7)
        dst_cell = h3.latlng_to_cell(dst_lat, dst_lon, 7)
        src_cell_int = int(src_cell, 16) if isinstance(src_cell, str) else src_cell
        dst_cell_int = int(dst_cell, 16) if isinstance(dst_cell, str) else dst_cell

        return Route(
            distance_meters=round(distance, 1),
            duration_seconds=round(duration, 1),
            from_cell=src_cell_int,
            to_cell=dst_cell_int,
            segments=n_points,
            path=tuple(path),
            profile=profile,
        )
