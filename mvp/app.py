"""ptiles MVP backend — pure stdlib HTTP server.
Starts a ptiles daemon subprocess for routing queries,
and shells out to ptiles CLI for buildings/roads bounds queries.
"""

import json
import subprocess
import random
import urllib.error
import urllib.request
import os
import signal
import time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode

# Import state bboxes from canonical source
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from states import STATES  # type: ignore

STATES_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
PTILES_CLI = Path("/home/aoi/kino/projects/timeline/target/release/ptiles")
OSRM_BASE = "https://routing.openstreetmap.de/routed-car/route/v1/driving"
PORT = 9352
DAEMON_PORT = 19353

# State bounding boxes: imported from scripts/states.py (canonical source)
STATE_BBOXES = {}
for s in STATES:
    STATE_BBOXES[s.abbr] = (s.min_lon, s.min_lat, s.max_lon, s.max_lat)


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


def state_roads_path(abbr):
    """Return path to a state's roads file if it exists."""
    p = STATES_DIR / f"{abbr}.roads.ptiles"
    return p if p.exists() else None


# ── Daemon subprocess management ──────────────────────────────

DAEMON_PROC = None


def start_daemon():
    """Start the ptiles daemon as a subprocess and wait for it to be ready."""
    global DAEMON_PROC
    print(f"Starting ptiles daemon on port {DAEMON_PORT}...", flush=True)
    DAEMON_PROC = subprocess.Popen(
        [str(PTILES_CLI), "daemon", str(STATES_DIR), str(DAEMON_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )

    # Poll /ping until the daemon is ready (up to 120s for 51-file load)
    start = time.monotonic()
    while time.monotonic() - start < 120:
        try:
            req = urllib.request.Request(f"http://localhost:{DAEMON_PORT}/ping")
            resp = urllib.request.urlopen(req, timeout=2)
            if resp.status == 200:
                print(f"Daemon ready after {time.monotonic() - start:.0f}s", flush=True)
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)

    print("ERROR: Daemon failed to start within 120s", flush=True)
    # Check stderr for clues
    if DAEMON_PROC.stderr:
        leftover = DAEMON_PROC.stderr.read(2048)
        if leftover:
            print(f"Daemon stderr: {leftover.decode(errors='replace')}", flush=True)
    return False


def stop_daemon():
    """Cleanly shut down the daemon subprocess."""
    global DAEMON_PROC
    if DAEMON_PROC and DAEMON_PROC.poll() is None:
        try:
            os.killpg(os.getpgid(DAEMON_PROC.pid), signal.SIGTERM)
            DAEMON_PROC.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(DAEMON_PROC.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        DAEMON_PROC = None


def daemon_get(url_path, query_params, timeout=30):
    """Make an HTTP GET to the daemon and return parsed JSON response."""
    qs = urlencode(query_params)
    url = f"http://localhost:{DAEMON_PORT}{url_path}?{qs}"
    try:
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return {"error": json.loads(body).get("error", f"HTTP {e.code}")}
        except json.JSONDecodeError:
            return {"error": f"HTTP {e.code}: {body}"}
    except urllib.error.URLError as e:
        return {"error": f"Daemon unreachable: {e.reason}"}
    except (OSError, ValueError) as e:
        return {"error": str(e)}


# ── HTTP handler ──────────────────────────────────────────────


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
            elif path == "/api/ping":
                self.json_response({"status": "ok"})
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

        profile = qs.get("profile", "driving")
        result = daemon_get(
            "/route",
            {
                "lat1": str(lat1),
                "lon1": str(lon1),
                "lat2": str(lat2),
                "lon2": str(lon2),
                "profile": profile,
            },
            timeout=120,
        )
        if "error" in result:
            self.json_response(result, 500)
        else:
            self.json_response(result)

    def route_osrm(self, qs):
        lat1 = self.get_param(qs, "lat1")
        lon1 = self.get_param(qs, "lon1")
        lat2 = self.get_param(qs, "lat2")
        lon2 = self.get_param(qs, "lon2")
        if None in (lat1, lon1, lat2, lon2):
            return self.json_response({"error": "Missing coords"}, 400)

        url = (
            f"{OSRM_BASE}/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ptiles-mvp/1.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            if data.get("code") != "Ok" or not data.get("routes"):
                return self.json_response(
                    {"error": data.get("message", "OSRM no route")}, 404
                )
            route = data["routes"][0]
            self.json_response(
                {
                    "distance_meters": route["distance"],
                    "duration_seconds": route["duration"],
                    "path": route["geometry"]["coordinates"],
                    "profile": "driving-osrm",
                }
            )
        except Exception as e:
            self.json_response({"error": f"OSRM failed: {e}"}, 502)

    def random_route(self):
        """Pick random coords from within US state bounding boxes (land only)."""
        abbr = random.choices(
            list(STATE_BBOXES.keys()),
            weights=[(b[2] - b[0]) * (b[3] - b[1]) for b in STATE_BBOXES.values()],
        )[0]
        min_lon, min_lat, max_lon, max_lat = STATE_BBOXES[abbr]
        lat1 = random.uniform(min_lat, max_lat)
        lon1 = random.uniform(min_lon, max_lon)
        lat2 = random.uniform(min_lat, max_lat)
        lon2 = random.uniform(min_lon, max_lon)
        self.json_response(
            {"origin": {"lat": lat1, "lon": lon1}, "dest": {"lat": lat2, "lon": lon2}}
        )

    def roads_bounds(self, qs):
        """Return road segments as GeoJSON from ptiles daemon bounds endpoint."""
        min_lat = self.get_param(qs, "min_lat")
        min_lon = self.get_param(qs, "min_lon")
        max_lat = self.get_param(qs, "max_lat")
        max_lon = self.get_param(qs, "max_lon")
        if None in (min_lat, min_lon, max_lat, max_lon):
            return self.json_response({"error": "Missing bounds"}, 400)

        span_lat = max_lat - min_lat
        span_lon = max_lon - min_lon
        if span_lat > 30.0 or span_lon > 30.0:
            return self.json_response({"error": "Bounds too large, zoom in"}, 400)

        # Use the CLI's bounds subcommand with a single roads file
        abbr = find_state((min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0)
        if not abbr:
            return self.json_response({"error": "No roads for this region"}, 404)
        rpath = state_roads_path(abbr)
        if not rpath:
            return self.json_response({"error": f"No roads file for {abbr}"}, 404)

        try:
            res = subprocess.run(
                [
                    str(PTILES_CLI),
                    str(rpath),
                    "bounds",
                    str(min_lat),
                    str(min_lon),
                    str(max_lat),
                    str(max_lon),
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode != 0:
                return self.json_response(
                    {"error": res.stderr or "ptiles bounds failed"}, 500
                )

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
                [
                    str(PTILES_CLI),
                    str(bpath),
                    "bounds",
                    str(min_lat),
                    str(min_lon),
                    str(max_lat),
                    str(max_lon),
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode != 0:
                return self.json_response(
                    {"error": res.stderr or "ptiles bounds failed"}, 500
                )

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
            return self.json_response(
                {"nearest": None, "error": "No buildings for this region"}, 404
            )
        bpath = state_buildings_path(abbr)
        if not bpath:
            return self.json_response(
                {"nearest": None, "error": f"No buildings file for {abbr}"}, 404
            )

        r = 0.003  # ~300m search radius
        try:
            res = subprocess.run(
                [
                    str(PTILES_CLI),
                    str(bpath),
                    "bounds",
                    str(lat - r),
                    str(lon - r),
                    str(lat + r),
                    str(lon + r),
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode != 0:
                return self.json_response(
                    {"nearest": None, "error": res.stderr or "ptiles failed"}, 500
                )

            lines = res.stdout.strip().split("\n")
            json_str = "\n".join(lines[6:]) if len(lines) > 6 else lines[-1]
            data = json.loads(json_str)
            features = data.get("features", [])

            if not features:
                return self.json_response({"nearest": None})

            best = None
            best_dist = float("inf")
            for f in features:
                coords = f.get("geometry", {}).get("coordinates", [[]])[0]
                if not coords:
                    continue
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
            self.json_response(
                {"nearest": None, "error": f"ptiles parse error: {e}"}, 500
            )

    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {args[0] if args else ''}")


# ── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    import atexit

    daemon_ok = start_daemon()
    atexit.register(stop_daemon)

    if not daemon_ok:
        print("FATAL: Could not start ptiles daemon. Exiting.")
        sys.exit(1)

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving at http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
