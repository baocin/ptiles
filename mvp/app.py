"""ptiles MVP backend — pure stdlib HTTP server."""
import json
import subprocess
import random
import urllib.request
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

STATES_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
BUILDINGS_PATH = Path("/home/aoi/kino/projects/ptiles/data/states/TN.buildings_v8.ptiles")
PTILES_CLI = Path("/home/aoi/kino/projects/timeline/target/debug/ptiles")
OSRM_BASE = "https://routing.openstreetmap.de/routed-car/route/v1/driving"
PORT = 9352

# US-wide random route zone
US_ROAD_ZONE = {"min_lat": 25.0, "max_lat": 49.0, "min_lon": -125.0, "max_lon": -67.0}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        qs = {}
        if "?" in self.path:
            for part in self.path.split("?")[1].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    qs[k] = v

        try:
            if path == "/":
                self.serve_file("static/index.html", "text/html")
            elif path == "/favicon.ico":
                self.serve_file("static/favicon.svg", "image/svg+xml")
            elif path.startswith("/static/"):
                self.serve_file(path.lstrip("/"), "text/html")
            elif path == "/api/route":
                self.route_ptiles(qs)
            elif path == "/api/route-osrm":
                self.route_osrm(qs)
            elif path == "/api/random-route":
                self.random_route()
            elif path == "/api/roads-bounds":
                self.roads_bounds(qs)
            elif path == "/api/buildings-bounds":
                self.buildings_bounds(qs)
            elif path == "/api/buildings":
                self.get_buildings(qs)
            else:
                self.send_error(404, "Not found")
        except Exception as e:
            self.json_response({"error": str(e)}, 500)

    def serve_file(self, rel_path, mime):
        file_path = Path(__file__).parent / rel_path
        if not file_path.exists():
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(file_path.read_bytes())

    def json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def get_param(self, qs, name, default=None):
        val = qs.get(name)
        if val is None:
            return default
        try:
            return float(val)
        except ValueError:
            return default

    def route_ptiles(self, qs):
        lat1 = self.get_param(qs, "lat1")
        lon1 = self.get_param(qs, "lon1")
        lat2 = self.get_param(qs, "lat2")
        lon2 = self.get_param(qs, "lon2")
        if None in (lat1, lon1, lat2, lon2):
            return self.json_response({"error": "Missing coords"}, 400)

        roads_path = str(STATES_DIR)
        if not Path(roads_path).is_dir():
            return self.json_response({"error": f"States directory not found"}, 404)

        try:
            profile = qs.get("profile", "driving")
            res = subprocess.run(
                [str(PTILES_CLI), "route", roads_path, str(lat1), str(lon1), str(lat2), str(lon2), "--json", f"--profile={profile}"],
                capture_output=True, text=True, timeout=30
            )
            if res.returncode != 0:
                return self.json_response({"error": res.stderr or "ptiles failed"}, 500)
            data = json.loads(res.stdout)
            data["profile"] = "driving"
            self.json_response(data)
        except subprocess.TimeoutExpired:
            self.json_response({"error": "ptiles timed out"}, 504)
        except json.JSONDecodeError:
            self.json_response({"error": "ptiles returned invalid JSON"}, 500)

    def route_osrm(self, qs):
        lat1 = self.get_param(qs, "lat1")
        lon1 = self.get_param(qs, "lon1")
        lat2 = self.get_param(qs, "lat2")
        lon2 = self.get_param(qs, "lon2")
        if None in (lat1, lon1, lat2, lon2):
            return self.json_response({"error": "Missing coords"}, 400)

        url = f"{OSRM_BASE}/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ptiles-mvp/1.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            if data.get("code") != "Ok" or not data.get("routes"):
                return self.json_response({"error": data.get("message", "OSRM no route")}, 404)
            route = data["routes"][0]
            self.json_response({
                "distance_meters": route["distance"],
                "duration_seconds": route["duration"],
                "path": route["geometry"]["coordinates"],
                "profile": "driving-osrm"
            })
        except Exception as e:
            self.json_response({"error": f"OSRM failed: {e}"}, 502)

    def random_route(self):
        """Pick random coords from denser road network zone for better ptiles success."""
        lat1 = random.uniform(US_ROAD_ZONE["min_lat"], US_ROAD_ZONE["max_lat"])
        lon1 = random.uniform(US_ROAD_ZONE["min_lon"], US_ROAD_ZONE["max_lon"])
        lat2 = random.uniform(US_ROAD_ZONE["min_lat"], US_ROAD_ZONE["max_lat"])
        lon2 = random.uniform(US_ROAD_ZONE["min_lon"], US_ROAD_ZONE["max_lon"])
        self.json_response({
            "origin": {"lat": lat1, "lon": lon1},
            "dest": {"lat": lat2, "lon": lon2}
        })

    def roads_bounds(self, qs):
        """Return road segments as GeoJSON from ptiles within given bounds."""
        min_lat = self.get_param(qs, "min_lat")
        min_lon = self.get_param(qs, "min_lon")
        max_lat = self.get_param(qs, "max_lat")
        max_lon = self.get_param(qs, "max_lon")
        if None in (min_lat, min_lon, max_lat, max_lon):
            return self.json_response({"error": "Missing bounds"}, 400)

        # Clamp to reasonable viewport to avoid giant responses
        span_lat = max_lat - min_lat
        span_lon = max_lon - min_lon
        if span_lat > 10.0 or span_lon > 10.0:
            return self.json_response({"error": "Bounds too large, zoom in"}, 400)

        roads_path = str(STATES_DIR)
        if not Path(roads_path).is_dir():
            return self.json_response({"error": "States directory not found"}, 404)

        try:
            res = subprocess.run(
                [str(PTILES_CLI), roads_path, "bounds",
                 str(min_lat), str(min_lon), str(max_lat), str(max_lon), "--json"],
                capture_output=True, text=True, timeout=30
            )
            if res.returncode != 0:
                return self.json_response({"error": res.stderr or "ptiles bounds failed"}, 500)

            # Strip header lines (first 5 lines + blank line = 6), rest is JSON
            lines = res.stdout.strip().split("\n")
            json_str = "\n".join(lines[6:]) if len(lines) > 6 else lines[-1]
            data = json.loads(json_str)
            self.json_response(data)
        except subprocess.TimeoutExpired:
            self.json_response({"error": "ptiles timed out"}, 504)
        except (json.JSONDecodeError, IndexError) as e:
            self.json_response({"error": f"ptiles bounds parse error: {e}"}, 500)

    def buildings_bounds(self, qs):
        """Return building footprints as GeoJSON from ptiles within given bounds."""
        min_lat = self.get_param(qs, "min_lat")
        min_lon = self.get_param(qs, "min_lon")
        max_lat = self.get_param(qs, "max_lat")
        max_lon = self.get_param(qs, "max_lon")
        if None in (min_lat, min_lon, max_lat, max_lon):
            return self.json_response({"error": "Missing bounds"}, 400)

        span_lat = max_lat - min_lat
        span_lon = max_lon - min_lon
        if span_lat > 2.0 or span_lon > 2.0:
            return self.json_response({"error": "Bounds too large, zoom in"}, 400)

        if not BUILDINGS_PATH.exists():
            return self.json_response({"error": "Buildings file not found"}, 404)

        try:
            res = subprocess.run(
                [str(PTILES_CLI), str(BUILDINGS_PATH), "bounds",
                 str(min_lat), str(min_lon), str(max_lat), str(max_lon), "--json"],
                capture_output=True, text=True, timeout=30
            )
            if res.returncode != 0:
                return self.json_response({"error": res.stderr or "ptiles bounds failed"}, 500)

            lines = res.stdout.strip().split("\n")
            json_str = "\n".join(lines[6:]) if len(lines) > 6 else lines[-1]
            data = json.loads(json_str)
            self.json_response(data)
        except subprocess.TimeoutExpired:
            self.json_response({"error": "ptiles timed out"}, 504)
        except (json.JSONDecodeError, IndexError) as e:
            self.json_response({"error": f"ptiles bounds parse error: {e}"}, 500)

    def get_buildings(self, qs):
        lat = self.get_param(qs, "lat")
        lon = self.get_param(qs, "lon")
        if lat is None or lon is None:
            return self.json_response({"buildings": []})
        # Reuse buildings_bounds with a small radius around the click point
        r = 0.003  # ~300m radius
        qs["min_lat"] = str(lat - r)
        qs["min_lon"] = str(lon - r)
        qs["max_lat"] = str(lat + r)
        qs["max_lon"] = str(lon + r)
        self.buildings_bounds(qs)

    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {args[0] if args else ''}")


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving at http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
