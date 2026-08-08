#!/usr/bin/env python3
"""Find a downloadable statewide parcel service for each state.

There is no national parcel dataset. NSGIC's 2023 survey puts it at 35 states
running an aggregation program and 22 publishing most of their parcels, so
partial coverage is the ceiling here, not a bug in this script.

Strategy: ArcGIS Hub indexes the public ArcGIS Online/Enterprise services that
most state GIS offices publish through, so one search per state finds
candidates without hand-collecting 51 portal URLs. Every candidate is then
probed directly -- a Hub record proves a service was listed, not that it
answers today, and not how many parcels it holds.

    discover_parcels.py <out_json> [--states TN,MT]
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from states import STATES, get_state

HUB_SEARCH = "https://hub.arcgis.com/api/v3/datasets"
UA = {"User-Agent": "ptiles-parcel-discovery/1.0"}
TIMEOUT = 60

# A hit must look like parcels, and must not look like a single county, a
# zoning overlay, or somebody's class project.
GOOD = re.compile(r"\bparcel", re.I)
BAD = re.compile(
    r"\b(county|city of|zoning|school|voting|precinct|historic|test|draft|"
    r"sample|training|old|archive|deprecated)\b",
    re.I,
)
STATEWIDE = re.compile(r"\b(statewide|state\s*wide|all\s+counties|state\s+parcel)\b", re.I)


def get_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def hub_search(state_name):
    q = urllib.parse.urlencode(
        {
            "q": f"{state_name} statewide parcels",
            "page[size]": 20,
            "fields[datasets]": "name,owner,url,recordCount,source",
        }
    )
    try:
        return get_json(f"{HUB_SEARCH}?{q}").get("data", [])
    except Exception as e:
        print(f"    hub search failed: {e}", file=sys.stderr)
        return []


def to_wgs84(extent):
    """Return (min_lon, min_lat, max_lon, max_lat) or None.

    Services publish either lon/lat (4326) or Web Mercator metres (3857 /
    102100); anything else is not worth guessing at for a sanity check.
    """
    try:
        xmin, ymin = float(extent["xmin"]), float(extent["ymin"])
        xmax, ymax = float(extent["xmax"]), float(extent["ymax"])
    except (KeyError, TypeError, ValueError):
        return None
    wkid = (extent.get("spatialReference") or {}).get("latestWkid") or (
        extent.get("spatialReference") or {}
    ).get("wkid")
    if wkid in (4326, None):
        return (xmin, ymin, xmax, ymax)
    if wkid in (3857, 102100, 900913):
        import math

        def unproject(x, y):
            lon = x / 20037508.34 * 180.0
            lat = y / 20037508.34 * 180.0
            lat = 180.0 / math.pi * (2 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2)
            return lon, lat

        a = unproject(xmin, ymin)
        b = unproject(xmax, ymax)
        return (a[0], a[1], b[0], b[1])
    return None


def probe(service_url, state):
    """Confirm the service answers, holds parcels, and covers THIS state.

    The state check is geographic, not textual. Matching on names alone
    assigned Montana's statewide service to Tennessee and Nebraska -- every
    hit that says "parcel" and lives on a .gov host looks alike, so the run
    reported full coverage while pointing three states at one file.
    """
    base = service_url.rstrip("/")
    # A layer URL ends in /<n>; a service URL does not. Only layers can count.
    if not re.search(r"/\d+$", base):
        try:
            info = get_json(f"{base}?f=json", timeout=30)
        except Exception as e:
            return None, f"service unreachable: {e}"
        layers = (info.get("layers") or []) + (info.get("subLayers") or [])
        if not layers:
            return None, "no layers"
        base = f"{base}/{layers[0].get('id', 0)}"

    try:
        meta = get_json(f"{base}?f=json", timeout=30)
    except Exception as e:
        return None, f"layer unreachable: {e}"

    box = to_wgs84(meta.get("extent") or {})
    if box is None:
        # State plane and other projections are common and not worth a local
        # reprojection table -- ask the server for the extent in lon/lat.
        try:
            q = get_json(
                f"{base}/query?where=1%3D1&returnExtentOnly=true&outSR=4326&f=json",
                timeout=45,
            )
            box = to_wgs84(q.get("extent") or {})
        except Exception:
            box = None
    if box is None:
        return None, "no usable extent (cannot confirm which state it covers)"
    lon0, lat0, lon1, lat1 = box
    # Require real overlap with the state box, and a centre inside it: a
    # neighbouring state's layer can clip the border, a national layer cannot
    # be centred on one state.
    if not (lon1 >= state.min_lon and lon0 <= state.max_lon
            and lat1 >= state.min_lat and lat0 <= state.max_lat):
        return None, f"extent {lon0:.1f},{lat0:.1f}..{lon1:.1f},{lat1:.1f} misses {state.abbr}"
    clon, clat = (lon0 + lon1) / 2, (lat0 + lat1) / 2
    if not (state.min_lon <= clon <= state.max_lon and state.min_lat <= clat <= state.max_lat):
        return None, f"centre {clat:.1f},{clon:.1f} outside {state.abbr}"

    try:
        cnt = get_json(
            f"{base}/query?where=1%3D1&returnCountOnly=true&f=json", timeout=45
        )
    except Exception as e:
        return None, f"count failed: {e}"
    if "count" not in cnt:
        return None, f"no count in response: {str(cnt)[:80]}"
    return {"layer_url": base, "count": cnt["count"], "extent": box}, None


def pick(state, hits):
    """Rank candidates: explicit 'statewide' first, then by parcel count."""
    scored = []
    for h in hits:
        a = h.get("attributes", {})
        name = a.get("name") or ""
        url = a.get("url") or ""
        if not url or not GOOD.search(name) or BAD.search(name):
            continue
        # Keep hits that name the state or are hosted on a .gov service.
        mentions_state = state.name.lower() in name.lower() or f"{state.abbr} " in name
        gov = ".gov" in url.lower()
        if not (mentions_state or gov):
            continue
        scored.append(
            (
                1 if STATEWIDE.search(name) else 0,
                1 if gov else 0,
                a.get("recordCount") or 0,
                name,
                url,
            )
        )
    scored.sort(reverse=True)
    return scored


def main():
    p = argparse.ArgumentParser()
    p.add_argument("out_json")
    p.add_argument("--states")
    args = p.parse_args()

    targets = (
        [s for s in (get_state(a.strip()) for a in args.states.split(",")) if s]
        if args.states
        else list(STATES)
    )

    found = {}
    for s in targets:
        print(f"  {s.abbr} {s.name}", flush=True)
        hits = hub_search(s.name)
        cands = pick(s, hits)
        result = None
        for _, _, _, name, url in cands[:4]:
            info, err = probe(url, s)
            if info and info["count"] > 0:
                result = {
                    "state": s.abbr,
                    "name": name,
                    "service_url": url,
                    "layer_url": info["layer_url"],
                    "count": info["count"],
                    "extent": info["extent"],
                }
                print(f"    OK  {name[:52]} -- {info['count']:,} features", flush=True)
                break
            print(f"    skip {name[:44]}: {err}", flush=True)
        if not result:
            print("    no working statewide service found", flush=True)
        found[s.abbr] = result
        time.sleep(1)  # be polite to Hub

    Path(args.out_json).write_text(json.dumps(found, indent=2))
    ok = sum(1 for v in found.values() if v)
    total = sum(v["count"] for v in found.values() if v)
    print(f"\nDISCOVERY_COMPLETE {ok}/{len(targets)} states, {total:,} parcels reachable")


if __name__ == "__main__":
    main()
