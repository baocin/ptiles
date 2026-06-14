#!/usr/bin/env python3
"""US v8: Use pmtiles tile CLI (batch mode) for extraction."""
import sys
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")

import os, struct, time, math, gc, shutil, subprocess, json
from collections import defaultdict
import h3
import mapbox_vector_tile
from shared import write_header, HEADER_SIZE, write_index, train_dictionary, compress_block
from encode_v8 import encode_block_v8

PMTILES = "/tmp/pmtiles"
SRC = "/home/aoi/data/protomaps/20260513.pmtiles"
OUT = "/home/aoi/kino/projects/ptiles/data/US.buildings_v8.ptiles"
TMP = "/tmp/ptiles_us_v8"
Z = 14
H3R = 7

def tile_center(z, x, y):
    n = 2.0 ** z
    lon = (x + 0.5) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
    return lon, lat

def lonlat_to_tile(lon, lat, z):
    n = 2.0 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat)) + 1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y

def get_tile(z, x, y):
    try:
        r = subprocess.run([PMTILES, "tile", SRC, str(z), str(x), str(y)],
                          capture_output=True, timeout=10)
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except:
        pass
    return None

def extract_buildings(tile_data, z, x, y):
    if not tile_data: return []
    try:
        result = mapbox_vector_tile.decode(tile_data)
    except:
        return []
    buildings = []
    tc_lon, tc_lat = tile_center(z, x, y)
    td = 360.0 / (2**z)
    for layer_name, layer in result.items():
        if layer_name != "building": continue
        for feat in layer.get("features", []):
            g = feat.get("geometry", {})
            if g.get("type") not in ("Polygon", "MultiPolygon"): continue
            props = feat.get("properties", {})
            if not props.get("building"): continue
            rings = g["coordinates"] if g["type"] == "Polygon" else g["coordinates"][0]
            if len(rings) < 4: continue
            outer = []
            for pt in rings:
                lon = tc_lon + (pt[0]/4096.0 - 0.5) * td
                lat = tc_lat + (0.5 - pt[1]/4096.0) * td
                outer.append([lon, lat])
            if outer[0] != outer[-1]: outer.append(outer[0])
            btype = str(props.get("building", "yes"))
            h = props.get("height")
            if h:
                try: h = float(h)
                except: h = None
            b = {"osm_id": props.get("@id", 0) or abs(hash(f"{z}/{x}/{y}/{len(buildings)}")) % 10**10,
                 "coords": outer, "building_type": btype, "height_m": h}
            nm = props.get("name")
            if nm: b["name"] = str(nm)
            buildings.append(b)
    return buildings

def flush_cells(cb):
    cc = {}
    for cell in cb:
        try:
            lat, lon = h3.cell_to_latlng(hex(cell)[2:])
            cc[cell] = (lon, lat)
        except: continue
    for cell, bldgs in cb.items():
        if cell not in cc: continue
        bldgs.sort(key=lambda b: b.get("osm_id", 0))
        try:
            blk, _ = encode_block_v8(bldgs, cell, cc)
        except: continue
        with open(os.path.join(TMP, f"{hex(cell)[2:]}.v8tmp"), "ab") as f:
            f.write(blk)
    cb.clear()

# Main
start = time.time()
os.makedirs(TMP, exist_ok=True)

x1, y1 = lonlat_to_tile(-125, 50, Z)
x2, y2 = lonlat_to_tile(-66, 24, Z)
total = (x2-x1+1)*(y2-y1+1)
print(f"US z14: x=[{x1},{x2}] y=[{y1},{y2}] = {total:,} tiles", flush=True)

cell_bldgs = defaultdict(list)
total_b = tiles_fetched = tiles_data = 0

for y in range(y1, y2+1):
    for x in range(x1, x2+1):
        tiles_fetched += 1
        if tiles_fetched % 1000 == 0:
            e = time.time()-start
            print(f"  {tiles_fetched}/{total} ({100*tiles_fetched/total:.1f}%) "
                  f"{tiles_fetched/e:.0f} t/s, {total_b:,} bldgs", flush=True)
        if tiles_fetched % 20000 == 0 and len(cell_bldgs) > 20000:
            flush_cells(cell_bldgs); gc.collect()
        td = get_tile(Z, x, y)
        if not td: continue
        bldgs = extract_buildings(td, Z, x, y)
        if not bldgs: continue
        tiles_data += 1
        for b in bldgs:
            lats = [c[1] for c in b["coords"]]
            lons = [c[0] for c in b["coords"]]
            lat = sum(lats)/len(lats)
            lon = sum(lons)/len(lons)
            ch = h3.latlng_to_cell(lat, lon, H3R)
            cell = int(ch, 16) if isinstance(ch, str) else int(ch)
            cell_bldgs[cell].append(b)
            total_b += 1

flush_cells(cell_bldgs)

e1 = time.time()-start
cells = [f for f in os.listdir(TMP) if f.endswith(".v8tmp")]
print(f"\nPass 1: {total_b:,} bldgs in {len(cells)} cells, {e1:.0f}s", flush=True)

# Pass 2
raw = {}
for fname in cells:
    cell = int(fname.replace(".v8tmp",""), 16)
    with open(os.path.join(TMP, fname), "rb") as f:
        raw[cell] = f.read()

samples = list(raw.values())[:2000]
print(f"Training dict on {len(samples)} samples...", flush=True)
dict_data = train_dictionary(samples)

cc = {}
for cell in raw:
    try:
        lat, lon = h3.cell_to_latlng(hex(cell)[2:])
        cc[cell] = (lon, lat)
    except: continue

comp = {}
sc = sorted(raw.keys())
for cell in sc:
    comp[cell] = compress_block(raw[cell], dict_data)

do = HEADER_SIZE
dl = len(dict_data)
io = do + dl
il = 4 + len(sc) * 19
bo = io + il

entries = []
roff = bo
tf = 0
for cell in sc:
    cb = comp[cell]
    r = raw[cell]
    if r:
        pos = 1; tc = r[0]
        for _ in range(tc):
            if pos >= len(r): break
            sl = r[pos]; pos += 1+sl
        cf = 0
        while pos+4 <= len(r):
            rl = struct.unpack_from("<I", r, pos)[0]
            pos += 4+rl; cf += 1
    else: cf = 0
    entries.append({"h3_cell": cell, "block_offset": roff,
                    "block_length": len(cb), "feature_count": min(cf, 65535)})
    roff += len(cb); tf += cf

alls_lat = [c[1] for c in cc.values()]
alls_lon = [c[0] for c in cc.values()]

with open(OUT, "wb") as f:
    write_header(f, b"PTILESF\x00", 8, min(alls_lat), min(alls_lon),
                 max(alls_lat), max(alls_lon), tf, len(comp),
                 do, dl, io, il, bo)
    f.write(dict_data)
    write_index(f, entries)
    for cell in sc:
        f.write(comp[cell])

ts = os.path.getsize(OUT)
print(f"\nDone: {OUT}", flush=True)
print(f"  Size: {ts:,}B ({ts/1e6:.1f}MB)", flush=True)
print(f"  Features: {tf:,}", flush=True)
print(f"  B/bldg: {ts/tf:.1f}" if tf else "", flush=True)
print(f"  Total: {time.time()-start:.0f}s", flush=True)

shutil.rmtree(TMP, ignore_errors=True)
