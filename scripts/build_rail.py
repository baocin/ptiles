1|#!/usr/bin/env python3
2|"""
3|Build rail.ptiles from per-state OSM PBF files.
4|
5|Uses osmium FileProcessor + KeyFilter for fast railway extraction.
6|Format: PTILEST magic, v1, v2 merged-block.
7|
8|Captures:
9|  - Railway ways (tracks): rail, tram, light_rail, subway, monorail, narrow_gauge, funicular, preserved
10|  - Station nodes: station, halt, tram_stop, subway_entrance
11|"""
12|
13|import sys
14|import struct
15|import time
16|
17|sys.stdout.reconfigure(line_buffering=True)
18|sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")
19|
20|from pathlib import Path
21|from collections import defaultdict
22|
23|import h3
24|import zstandard as zstd
25|
26|from shared import (
27|    encode_varint,
28|    zigzag_encode,
29|    encode_index_entry_v2,
30|    INDEX_ENTRY_SIZE_V2,
31|    encode_merged_block,
32|    write_header,
33|    HEADER_SIZE,
34|)
35|from states import STATES, get_state
36|
37|OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
38|PBF_DIR = Path("/mnt/aoi/kino/ptiles/pbfs")
39|MAGIC = b"PTILEST\x00"
40|VERSION = 1
41|H3_RES = 7
42|
43|PBF_MAP = {
44|    "AL": "alabama",
45|    "AK": "alaska",
46|    "AZ": "arizona",
47|    "AR": "arkansas",
48|    "CA": "california",
49|    "CO": "colorado",
50|    "CT": "connecticut",
51|    "DE": "delaware",
52|    "DC": "district-of-columbia",
53|    "FL": "florida",
54|    "GA": "georgia",
55|    "HI": "hawaii",
56|    "ID": "idaho",
57|    "IL": "illinois",
58|    "IN": "indiana",
59|    "IA": "iowa",
60|    "KS": "kansas",
61|    "KY": "kentucky",
62|    "LA": "louisiana",
63|    "ME": "maine",
64|    "MD": "maryland",
65|    "MA": "massachusetts",
66|    "MI": "michigan",
67|    "MN": "minnesota",
68|    "MS": "mississippi",
69|    "MO": "missouri",
70|    "MT": "montana",
71|    "NE": "nebraska",
72|    "NV": "nevada",
73|    "NH": "new-hampshire",
74|    "NJ": "new-jersey",
75|    "NM": "new-mexico",
76|    "NY": "new-york",
77|    "NC": "north-carolina",
78|    "ND": "north-dakota",
79|    "OH": "ohio",
80|    "OK": "oklahoma",
81|    "OR": "oregon",
82|    "PA": "pennsylvania",
83|    "RI": "rhode-island",
84|    "SC": "south-carolina",
85|    "SD": "south-dakota",
86|    "TN": "tennessee",
87|    "TX": "texas",
88|    "UT": "utah",
89|    "VT": "vermont",
90|    "VA": "virginia",
91|    "WA": "washington",
92|    "WV": "west-virginia",
93|    "WI": "wisconsin",
94|    "WY": "wyoming",
95|}
96|
97|# Track types we capture (as ways with linestring geometry)
98|TRACK_TYPES = {
99|    "rail",
100|    "tram",
101|    "light_rail",
102|    "subway",
103|    "monorail",
104|    "narrow_gauge",
105|    "funicular",
106|    "preserved",
107|}
108|# Station types we capture (as nodes with point geometry)
109|STATION_TYPES = {"station", "halt", "tram_stop", "subway_entrance"}
110|
111|RT = [
112|    "rail",
113|    "subway",
114|    "light_rail",
115|    "tram",
116|    "monorail",
117|    "narrow_gauge",
118|    "funicular",
119|    "station",
120|    "halt",
121|    "tram_stop",
122|    "subway_entrance",
123|]
124|RTI = {t: i for i, t in enumerate(RT)}
125|
126|
127|def extract(pbf):
128|    import osmium
129|
130|    fp = osmium.FileProcessor(pbf).with_filter(osmium.filter.KeyFilter("railway"))
131|    features = []
132|    for obj in fp:
133|        is_node = hasattr(obj, "lat")
134|        rail_type = None
135|        name = None
136|        for t in obj.tags:
137|            if t.k == "railway" and t.v:
138|                if t.v in TRACK_TYPES or t.v in STATION_TYPES:
139|                    rail_type = t.v
140|            elif t.k == "name":
141|                name = t.v
142|        if not rail_type:
143|            continue
144|
145|        geom_type = 0  # 0=linestring, 1=point
146|        if rail_type in STATION_TYPES and is_node:
147|            geom_type = 1
148|
149|        if geom_type == 0:
150|            # Linestring: extract way nodes
151|            if not hasattr(obj, "nodes"):
152|                continue
153|            coords = []
154|            try:
155|                for n in obj.nodes:
156|                    if n.location.valid():
157|                        coords.append((n.lon, n.lat))
158|            except Exception:
159|                continue
160|            if len(coords) < 2:
161|                continue
162|            key_coord = coords[0]
163|        else:
164|            # Point: station node
165|            key_coord = (obj.lon, obj.lat)
166|
167|        try:
168|            cell = h3.latlng_to_cell(key_coord[1], key_coord[0], H3_RES)
169|        except Exception:
170|            continue
171|
172|        features.append(
173|            {
174|                "osm_id": obj.id,
175|                "rail_type": rail_type,
176|                "geom_type": geom_type,
177|                "coords": coords if geom_type == 0 else [(obj.lon, obj.lat)],
178|                "name": name,
179|                "cell": int(cell, 16) if isinstance(cell, str) else cell,
180|            }
181|        )
182|
183|    return features
184|
185|
186|def encode_coordinates(coords):
187|    """Encode coordinate sequence as varint deltas."""
188|    if not coords:
189|        return b""
190|    buf = bytearray()
191|    first_lon = round(coords[0][0] * 100_000)
192|    first_lat = round(coords[0][1] * 100_000)
193|    prev_lon = first_lon
194|    prev_lat = first_lat
195|    for lon, lat in coords[1:]:
196|        cur_lon = round(lon * 100_000)
197|        cur_lat = round(lat * 100_000)
198|        buf.extend(encode_varint(zigzag_encode(cur_lon - prev_lon)))
199|        buf.extend(encode_varint(zigzag_encode(cur_lat - prev_lat)))
200|        prev_lon, prev_lat = cur_lon, cur_lat
201|    return bytes(buf)
202|
203|
204|def enc(feat, pid):
205|    buf = bytearray()
206|    buf.extend(encode_varint(zigzag_encode(feat["osm_id"] - pid)))
207|    buf.append(feat["geom_type"])  # 0=linestring, 1=point
208|    if feat["geom_type"] == 0:
209|        # Linestring: vertex count + first absolute + deltas
210|        coords = feat.get("coords", [])
211|        if len(coords) < 2:
212|            return b""
213|        n_verts = len(coords)
214|        buf.extend(struct.pack("<H", n_verts))
215|        buf.extend(
216|            struct.pack(
217|                "<ii", round(coords[0][0] * 100000), round(coords[0][1] * 100000)
218|            )
219|        )
220|        buf.extend(encode_coordinates(coords))
221|    else:
222|        # Point: lon, lat
223|        pt = feat.get("coords", [(0, 0)])[0]
224|        buf.extend(struct.pack("<ii", round(pt[0] * 100000), round(pt[1] * 100000)))
225|    # Rail type index
226|    buf.append(RTI.get(feat["rail_type"], 0))
227|    # Flags byte
228|    flags = 0
229|    if feat.get("name"):
230|        flags |= 0x01
231|    buf.append(flags)
232|    if feat.get("name"):
233|        nb = feat["name"].encode("utf-8")
234|        buf.extend(struct.pack("<H", len(nb)))
235|        buf.extend(nb)
236|    return bytes(buf)
237|
238|
239|def build_state(abbr):
240|    pbfn = PBF_MAP.get(abbr)
241|    if not pbfn:
242|        return {"abbr": abbr, "error": "no mapping"}
243|    pbfp = PBF_DIR / f"{pbfn}-latest.osm.pbf"
244|    if not pbfp.exists():
245|        return {"abbr": abbr, "error": "no pbf"}
246|    s = get_state(abbr)
247|    t0 = time.time()
248|
249|    features = extract(str(pbfp))
250|    if not features:
251|        return {"abbr": abbr, "features": 0, "time_s": round(time.time() - t0, 1)}
252|
253|    pc = defaultdict(list)
254|    for f in features:
255|        cell_hex = hex(f["cell"])[2:] if isinstance(f["cell"], int) else str(f["cell"])
256|        pc[cell_hex].append(f)
257|    for c in pc:
258|        pc[c].sort(key=lambda p: p["osm_id"])
259|
260|    sc = sorted(pc.keys())
261|    mb = []
262|    pi = []
263|    bs = 8
264|    for i in range(0, len(sc), bs):
265|        bch = sc[i : i + bs]
266|        cr = []
267|        pd = []
268|        for cell in bch:
269|            rs = []
270|            pid = 0
271|            for f in pc[cell]:
272|                rs.append(enc(f, pid))
273|                pid = f["osm_id"]
274|            cr.append((int(cell, 16), rs))
275|            pd.append((int(cell, 16), len(rs)))
276|        if not cr:
277|            continue
278|        cha = hex(int(bch[0], 16))[2:]
279|        cla, clo = h3.cell_to_latlng(cha)
280|        if not isinstance(clo, (int, float)) or not isinstance(cla, (int, float)):
281|            continue
282|        blk = encode_merged_block(cr, round(clo * 100000), round(cla * 100000))
283|        mb.append(blk)
284|        off = 0
285|        for cell, cnt in pd:
286|            pi.append(
287|                {
288|                    "h3_cell": cell,
289|                    "block_offset": 0,
290|                    "block_length": len(blk),
291|                    "feature_count": cnt,
292|                    "cell_index": off,
293|                    "min_lon": round(
294|                        min(f["coords"][0][0] for f in pc[hex(cell)[2:]]) * 100000
295|                    )
296|                    if pc.get(hex(cell)[2:])
297|                    else 0,
298|                    "min_lat": round(
299|                        min(f["coords"][0][1] for f in pc[hex(cell)[2:]]) * 100000
300|                    )
301|                    if pc.get(hex(cell)[2:])
302|                    else 0,
303|                    "max_lon": round(
304|                        max(f["coords"][0][0] for f in pc[hex(cell)[2:]]) * 100000
305|                    )
306|                    if pc.get(hex(cell)[2:])
307|                    else 0,
308|                    "max_lat": round(
309|                        max(f["coords"][0][1] for f in pc[hex(cell)[2:]]) * 100000
310|                    )
311|                    if pc.get(hex(cell)[2:])
312|                    else 0,
313|                }
314|            )
315|            off += 1
316|
317|    if not mb:
318|        return {"abbr": abbr, "features": 0}
319|
320|    # Skip dictionary training for sparse rail data; compress without dict
321|    dd = b""
322|    cbs = [zstd.ZstdCompressor(level=1).compress(b) for b in mb]
323|
324|    tf = sum(e["feature_count"] for e in pi)
325|    do = HEADER_SIZE
326|    dl = len(dd)
327|    io = do + dl
328|    il = 4 + len(pi) * INDEX_ENTRY_SIZE_V2
329|    bo = io + il
330|
331|    ie = []
332|    for idx, pc_ in enumerate(pi):
333|        block_idx = idx // bs
334|        ie.append(
335|            {
336|                **pc_,
337|                "block_offset": bo + sum(len(cb) for cb in cbs[:block_idx]),
338|                "block_length": len(cbs[block_idx]) if block_idx < len(cbs) else 0,
339|            }
340|        )
341|
342|    op = OUTPUT_DIR / f"{abbr}.rail.ptiles"
343|    with open(op, "wb") as f:
344|        write_header(
345|            f,
346|            MAGIC,
347|            VERSION,
348|            s.min_lat,
349|            s.min_lon,
350|            s.max_lat,
351|            s.max_lon,
352|            tf,
353|            len(mb),
354|            do,
355|            dl,
356|            io,
357|            il,
358|            bo,
359|        )
360|        f.write(dd)
361|        f.write(struct.pack("<I", len(ie)))
362|        for e in ie:
363|            f.write(
364|                encode_index_entry_v2(
365|                    e["h3_cell"],
366|                    e["min_lon"],
367|                    e["min_lat"],
368|                    e["max_lon"],
369|                    e["max_lat"],
370|                    e["block_offset"],
371|                    e["block_length"],
372|                    e["feature_count"],
373|                    e["cell_index"],
374|                )
375|            )
376|        for cb in cbs:
377|            f.write(cb)
378|
379|    sz = op.stat().st_size
380|    return {
381|        "abbr": abbr,
382|        "features": tf,
383|        "cells": len(pc),
384|        "bytes": sz,
385|        "time_s": round(time.time() - t0, 1),
386|    }
387|
388|
389|def main():
390|    import argparse
391|
392|    p = argparse.ArgumentParser()
393|    p.add_argument("--all", action="store_true")
394|    p.add_argument("--states")
395|    args = p.parse_args()
396|    targets = []
397|    if args.all:
398|        targets = [s.abbr for s in STATES]
399|    elif args.states:
400|        for a in args.states.split(","):
401|            s = get_state(a.strip())
402|            if s:
403|                targets.append(s.abbr)
404|    else:
405|        p.print_help()
406|        return
407|    for abbr in targets:
408|        try:
409|            r = build_state(abbr)
410|            if r and r.get("features"):
411|                print(
412|                    f"  {r['abbr']:2s} {r['features']:6d} features  {r['bytes']:10,d} B  {r['time_s']:6.1f}s",
413|                    flush=True,
414|                )
415|            elif r:
416|                print(
417|                    f"  {r['abbr']:2s}  {r.get('features', 0)}  ({r.get('error', '')})",
418|                    flush=True,
419|                )
420|        except Exception as e:
421|            print(f"  ERROR {abbr}: {e}", flush=True)
422|            import traceback
423|
424|            traceback.print_exc()
425|
426|
427|if __name__ == "__main__":
428|    main()
429|