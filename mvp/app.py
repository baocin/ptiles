"""ptiles MVP backend — pure stdlib HTTP server."""
import json
import subprocess
import random
import urllib.request
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

STATES_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
PTILES_CLI = Path("/tmp/ptiles-target/debug/ptiles")
OSRM_BASE = "https://routing.openstreetmap.de/routed-car/route/v1/driving"
PORT = 9352

# State bounding boxes (min_lon, min_lat, max_lon, max_lat) for building file lookup.
STATE_BBOXES = {
    "AL": (-88.5, 30.0, -84.9, 35.0), "AR": (-94.6, 33.0, -89.0, 36.5),
    "AZ": (-115.0, 31.3, -109.0, 37.0), "CA": (-124.5, 32.5, -114.1, 42.0),
    "CO": (-109.1, 37.0, -102.0, 41.0), "CT": (-73.7, 40.9, -71.8, 42.1),
    "DC": (-77.2, 38.8, -76.9, 39.0), "DE": (-75.8, 38.4, -75.0, 39.9),
    "FL": (-87.6, 24.4, -80.0, 31.0), "GA": (-85.6, 30.0, -78.4, 35.0),
    "IA": (-96.6, 40.4, -90.1, 43.5), "ID": (-117.0, 42.0, -111.0, 49.0),
    "IL": (-91.5, 36.9, -87.0, 42.5), "IN": (-88.1, 37.8, -84.8, 41.8),
    "KS": (-102.1, 37.0, -94.6, 40.0), "KY": (-89.6, 36.5, -82.0, 39.2),
    "LA": (-94.1, 28.9, -88.8, 33.0), "MA": (-73.5, 41.2, -69.9, 42.9),
    "MD": (-79.5, 37.9, -75.0, 39.8), "ME": (-71.1, 43.0, -66.9, 47.5),
    "MI": (-90.4, 41.7, -82.4, 48.3), "MN": (-97.3, 43.5, -89.5, 49.4),
    "MO": (-95.8, 35.9, -89.1, 40.6), "MS": (-91.7, 30.0, -88.1, 35.0),
    "MT": (-116.1, 44.4, -104.0, 49.0), "NC": (-84.3, 33.8, -75.4, 36.6),
    "ND": (-104.1, 45.9, -96.5, 49.0), "NE": (-104.1, 40.0, -95.3, 43.0),
    "NH": (-72.6, 42.7, -70.6, 45.3), "NJ": (-75.6, 38.9, -73.9, 41.4),
    "NM": (-109.1, 31.3, -103.0, 37.0), "NV": (-120.0, 35.0, -114.0, 42.0),
    "NY": (-79.8, 40.5, -71.8, 45.0), "OH": (-84.8, 38.4, -80.5, 41.7),
    "OK": (-103.0, 33.6, -94.4, 37.0), "OR": (-124.6, 41.9, -116.5, 46.3),
    "PA": (-80.5, 39.7, -74.7, 42.3), "RI": (-71.9, 41.1, -71.1, 42.0),
    "SC": (-83.4, 32.0, -78.5, 35.2), "SD": (-104.1, 42.5, -96.4, 45.9),
    "TN": (-90.3, 34.9, -81.6, 36.7), "TX": (-106.7, 25.8, -93.5, 36.5),
    "UT": (-114.1, 37.0, -109.0, 42.0), "VA": (-83.7, 36.5, -75.2, 39.5),
    "VT": (-73.5, 42.7, -71.5, 45.0), "WA": (-124.8, 45.5, -116.9, 49.0),
    "WI": (-93.0, 42.5, -86.8, 47.3), "WV": (-82.7, 37.2, -77.7, 40.6),
    "WY": (-111.1, 41.0, -104.0, 45.0),
}

# US-wide random route zone
US_ROAD_ZONE = {"min_lat": 25.0, "max_lat": 49.0, "min_lon": -125.0, "max_lon": -67.0}

def find_state(lat, lon):
    """Return the state abbreviation that contains (lat, lon), or None."""
    for abbr, (min_lon, min_lat, max_lon, max_lat) in STATE_BBOXES.items():
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return abbr
    return None

def state_buildings_path(abbr):
    """Return path to a state's buildings file if it exists."""
    p = STATES_DIR / f"{abbr}.buildings_v8.ptiles"
    return p if p.exists() else None


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
        if span_lat > 30.0 or span_lon > 30.0:
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

        mid_lat = (min_lat + max_lat) / 2.0
        mid_lon = (min_lon + max_lon) / 2.0
        abbr = find_state(mid_lat, mid_lon)
        if not abbr:
            return self.json_response({"error": "No buildings for this region"}, 404)
        bpath = state_buildings_path(abbr)
        if not bpath:
            return self.json_response({"error": f"No buildings file for {abbr}"}, 404)

        try:
            res = subprocess.run(
                [str(PTILES_CLI), str(bpath), "bounds",
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
        """Return the single nearest building at (lat, lon) as a GeoJSON Feature."""
        lat = self.get_param(qs, "lat")
        lon = self.get_param(qs, "lon")
        if lat is None or lon is None:
            return self.json_response({"nearest": None, "error": "Missing coords"}, 400)

        abbr = find_state(lat, lon)
        if not abbr:
            return self.json_response({"nearest": None, "error": "No buildings for this region"}, 404)
        bpath = state_buildings_path(abbr)
        if not bpath:
            return self.json_response({"nearest": None, "error": f"No buildings file for {abbr}"}, 404)

        r = 0.003  # ~300m search radius
        try:
            res = subprocess.run(
                [str(PTILES_CLI), str(bpath), "bounds",
                 str(lat - r), str(lon - r), str(lat + r), str(lon + r), "--json"],
                capture_output=True, text=True, timeout=30
            )
            if res.returncode != 0:
                return self.json_response({"nearest": None, "error": res.stderr or "ptiles failed"}, 500)

            lines = res.stdout.strip().split("\n")
            json_str = "\n".join(lines[6:]) if len(lines) > 6 else lines[-1]
            data = json.loads(json_str)
            features = data.get("features", [])

            if not features:
                return self.json_response({"nearest": None})

            # Find nearest by centroid distance
            best = None
            best_dist = float("inf")
            for f in features:
                coords = f.get("geometry", {}).get("coordinates", [[]])[0]
                if not coords:
                    continue
                # centroid of polygon
                cx = sum(c[0] for c in coords) / len(coords)
                cy = sum(c[1] for c in coords) / len(coords)
                d = (cx - lon) ** 2 + (cy - lat) ** 2
                if d < best_dist:
                    best_dist = d
                    best = f

            self.json_response({"nearest": best})
        except subprocess.TimeoutExpired:
            self.json_response({"nearest": None, "error": "ptiles timed out"}, 504)
        except (json.JSONDecodeError, IndexError) as e:
            self.json_response({"nearest": None, "error": f"ptiles parse error: {e}"}, 500)

    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {args[0] if args else ''}")


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving at http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
