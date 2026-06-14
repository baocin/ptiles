1|#!/usr/bin/env python3
2|"""
3|Build per-state PTILES v8 buildings from per-state OSM PBF.
4|
5|Usage:
6|    python build_state_v8.py TN
7|    python build_state_v8.py --all
8|"""
9|import sys, os, struct, time, gc, json
10|from pathlib import Path
11|from collections import defaultdict
12|
13|sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
14|import osmium
15|import h3
16|import zstandard as zstd
17|import numpy as np
18|
19|from shared import (
20|    write_header, HEADER_SIZE, write_index,
21|    train_dictionary, compress_block,
22|    encode_string_table, encode_table_ref,
23|)
24|from encode_v8 import (
25|    encode_building_v8, encode_block_v8,
26|    classify_height_tier, classify_use,
27|)
28|from states import STATES, get_state, state_bbox
29|
30|PBF_DIR = Path("/mnt/aoi/kino/ptiles/pbfs")
31|OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
32|H3_RES = 7
33|MAGIC = b"PTILESF\x00"
34|VERSION = 8
35|
36|STATE_PBF_NAMES = {
37|    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
38|    "CA": "california", "CO": "colorado", "CT": "connecticut",
39|    "DE": "delaware", "DC": "district-of-columbia", "FL": "florida",
40|    "GA": "georgia", "HI": "hawaii", "ID": "idaho", "IL": "illinois",
41|    "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky",
42|    "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
43|    "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri",
44|    "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new-hampshire",
45|    "NJ": "new-jersey", "NM": "new-mexico", "NY": "new-york",
46|    "NC": "north-carolina", "ND": "north-dakota", "OH": "ohio",
47|    "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
48|    "RI": "rhode-island", "SC": "south-carolina", "SD": "south-dakota",
49|    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
50|    "VA": "virginia", "WA": "washington", "WV": "west-virginia",
51|    "WI": "wisconsin", "WY": "wyoming",
52|}
53|
54|class BuildingHandler(osmium.SimpleHandler):
55|    def __init__(self, state_bbox):
56|        super().__init__()
57|        self.min_lon, self.min_lat, self.max_lon, self.max_lat = state_bbox
58|        self.buildings = []
59|
60|    def way(self, w):
61|        if not any(tag.k == 'building' for tag in w.tags if tag.v):
62|            return
63|        if not w.nodes:
64|            return
65|        try:
66|            lon_sum, lat_sum = 0.0, 0.0
67|            ring = []
68|            for node in w.nodes:
69|                try:
70|                    lat = node.location.lat
71|                    lon = node.location.lon
72|                except Exception:
73|                    continue
74|                if not ring:
75|                    if not (self.min_lon <= lon <= self.max_lon and
76|                            self.min_lat <= lat <= self.max_lat):
77|                        return
78|                ring.append([lon, lat])
79|                lon_sum += lon
80|                lat_sum += lat
81|            if len(ring) < 4:
82|                return
83|            if ring[0] != ring[-1]:
84|                ring.append(ring[0])
85|
86|            btype = "yes"
87|            name = None
88|            height = None
89|            for tag in w.tags:
90|                if tag.k == 'building' and tag.v:
91|                    btype = tag.v
92|                elif tag.k == 'name':
93|                    name = tag.v
94|                elif tag.k == 'height':
95|                    try:
96|                        height = float(tag.v) if tag.v else None
97|                    except ValueError:
98|                        height = None
99|
100|            self.buildings.append({
101|                "osm_id": w.id,
102|                "coords": ring,
103|                "building_type": btype,
104|                "height_m": height,
105|            })
106|            if name:
107|                self.buildings[-1]["name"] = name
108|        except Exception:
109|            pass
110|
111|def build_state_pbf(state):
112|    print(f"\n=== {state.abbr} {state.name} ===", flush=True)
113|    t0 = time.time()
114|
115|    pbf_name = STATE_PBF_NAMES.get(state.abbr)
116|    if not pbf_name:
117|        print(f"  No PBF file mapping for {state.abbr}")
118|        return
119|    pbf_path = PBF_DIR / f"{pbf_name}-latest.osm.pbf"
120|    if not pbf_path.exists():
121|        print(f"  PBF not found: {pbf_path}")
122|        return
123|
124|    bbox = state_bbox(state)
125|    handler = BuildingHandler(bbox)
126|    handler.apply_file(str(pbf_path), locations=True)
127|
128|    bldgs = handler.buildings
129|    if not bldgs:
130|        print("  No buildings found", flush=True)
131|        return
132|
133|    print(f"  Extracted {len(bldgs)} buildings", flush=True)
134|    bldgs.sort(key=lambda b: b["osm_id"])
135|
136|    # Group by H3 cell
137|    cells = defaultdict(list)
138|    for b in bldgs:
139|        lon, lat = b["coords"][0]
140|        cell = h3.latlng_to_cell(lat, lon, H3_RES)
141|        cells[int(cell, 16)].append(b)
142|    print(f"  Grouped into {len(cells)} H3 cells", flush=True)
143|
144|    # Encode blocks
145|    sorted_cells = sorted(cells.keys())
146|    raw_blocks = {}
147|    total_features = 0
148|    index_entries = []
149|    for cell in sorted_cells:
150|        block_bytes, count = encode_block_v8(cells[cell], cell)
151|        raw_blocks[cell] = block_bytes
152|        total_features += count
153|        # NOTE: index_entries populated after compression (need block sizes)
154|
155|    print(f"  Encoded {total_features} features in {len(raw_blocks)} blocks", flush=True)
156|
157|    # Train dict and compress
158|    samples = list(raw_blocks.values())[:2000]
159|    dict_data = train_dictionary(samples)
160|    compressed = {}
161|    for cell in sorted_cells:
162|        compressed[cell] = compress_block(raw_blocks[cell], dict_data)
163|
164|    # Build header
165|    dict_offset = HEADER_SIZE
166|    dict_length = len(dict_data)
167|    index_offset = dict_offset + dict_length
168|    
169|    # Build index entries — track running block offset relative to blocks_offset
170|    cur_block_off = 0
171|    for cell in sorted_cells:
172|        blen = len(compressed[cell])
173|        index_entries.append({
174|            "h3_cell": cell,
175|            "block_offset": cur_block_off,
176|            "block_length": blen,
177|            "feature_count": len(cells[cell]),
178|        })
179|        cur_block_off += blen
180|
181|    index_length = 4 + len(index_entries) * 19
182|    blocks_offset = index_offset + index_length
183|
184|    # Bbox
185|    all_lats, all_lons = [], []
186|    for cell in sorted_cells:
187|        lat, lon = h3.cell_to_latlng(hex(cell)[2:])
188|        all_lats.append(lat)
189|        all_lons.append(lon)
190|
191|    # Write file
192|    out_path = OUTPUT_DIR / f"{state.abbr}.buildings_v8.ptiles"
193|    with open(out_path, "wb") as f:
194|        write_header(f, MAGIC, VERSION, min(all_lats), min(all_lons),
195|                     max(all_lats), max(all_lons), total_features, len(compressed),
196|                     dict_offset, dict_length, index_offset, index_length, blocks_offset)
197|        # Write dict at dict_offset (already skipped by header)
198|        f.seek(dict_offset)
199|        f.write(dict_data)
200|        # Write index
201|        f.seek(index_offset)
202|        write_index(f, index_entries)
203|        # Write compressed blocks
204|        f.seek(blocks_offset)
205|        for cell in sorted_cells:
206|            f.write(compressed[cell])
207|
208|    dt = time.time() - t0
209|    sz = out_path.stat().st_size
210|    print(f"  Wrote {sz:,} bytes in {dt:.1f}s", flush=True)
211|    return {"abbr": state.abbr, "buildings": total_features, "cells": len(cells), "bytes": sz, "time_s": round(dt, 1)}
212|
213|def main():
214|    import argparse
215|    p = argparse.ArgumentParser()
216|    p.add_argument("target", nargs="?")
217|    p.add_argument("--all", action="store_true")
218|    args = p.parse_args()
219|
220|    targets = []
221|    if args.all:
222|        targets = [s for s in STATES if s.abbr in STATE_PBF_NAMES]
223|    elif args.target:
224|        s = get_state(args.target)
225|        if s:
226|            targets = [s]
227|        else:
228|            print(f"Unknown: {args.target}")
229|            return
230|    else:
231|        p.print_help()
232|        return
233|
234|    results = []
235|    for s in targets:
236|        try:
237|            r = build_state_pbf(s)
238|            if r:
239|                results.append(r)
240|        except Exception as e:
241|            print(f"ERROR {s.abbr}: {e}", flush=True)
242|            import traceback
243|            traceback.print_exc()
244|
245|    if results:
246|        print("\n=== SUMMARY ===")
247|        for r in results:
248|            print(f"  {r['abbr']:2s} {r['buildings']:8d} bldgs  {r.get('cells',0):4d} cells  {r.get('bytes',0):10,d} B  {r.get('time_s',0):6.1f}s")
249|
250|if __name__ == "__main__":
251|    main()
252|