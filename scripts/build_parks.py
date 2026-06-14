1|#!/usr/bin/env python3
2|"""
3|Build parks.ptiles from per-state OSM PBF files.
4|
5|Format: PTILESN magic, v1, v2 merged-block.
6|
7|Captures polygon features tagged as:
8|  leisure=park, leisure=golf_course, leisure=nature_reserve,
9|  leisure=recreation_ground, leisure=playground,
10|  boundary=national_park, boundary=protected_area
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
22|import osmium
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
39|MAGIC = b"PTILESN\x00"
40|VERSION = 1
41|H3_RES = 7
42|
43|PARK_TAGS = {
44|    "leisure": {
45|        "park",
46|        "golf_course",
47|        "nature_reserve",
48|        "recreation_ground",
49|        "playground",
50|    },
51|    "boundary": {"national_park", "protected_area"},
52|}
53|
54|PBF_MAP = {
55|    "AL": "alabama",
56|    "AK": "alaska",
57|    "AZ": "arizona",
58|    "AR": "arkansas",
59|    "CA": "california",
60|    "CO": "colorado",
61|    "CT": "connecticut",
62|    "DE": "delaware",
63|    "DC": "district-of-columbia",
64|    "FL": "florida",
65|    "GA": "georgia",
66|    "HI": "hawaii",
67|    "ID": "idaho",
68|    "IL": "illinois",
69|    "IN": "indiana",
70|    "IA": "iowa",
71|    "KS": "kansas",
72|    "KY": "kentucky",
73|    "LA": "louisiana",
74|    "ME": "maine",
75|    "MD": "maryland",
76|    "MA": "massachusetts",
77|    "MI": "michigan",
78|    "MN": "minnesota",
79|    "MS": "mississippi",
80|    "MO": "missouri",
81|    "MT": "montana",
82|    "NE": "nebraska",
83|    "NV": "nevada",
84|    "NH": "new-hampshire",
85|    "NJ": "new-jersey",
86|    "NM": "new-mexico",
87|    "NY": "new-york",
88|    "NC": "north-carolina",
89|    "ND": "north-dakota",
90|    "OH": "ohio",
91|    "OK": "oklahoma",
92|    "OR": "oregon",
93|    "PA": "pennsylvania",
94|    "RI": "rhode-island",
95|    "SC": "south-carolina",
96|    "SD": "south-dakota",
97|    "TN": "tennessee",
98|    "TX": "texas",
99|    "UT": "utah",
100|    "VT": "vermont",
101|    "VA": "virginia",
102|    "WA": "washington",
103|    "WV": "west-virginia",
104|    "WI": "wisconsin",
105|    "WY": "wyoming",
106|}
107|
108|
109|class ParkHandler(osmium.SimpleHandler):
110|    """Extract park polygons from ways and multipolygon relations."""
111|
112|    def __init__(self):
113|        super().__init__()
114|        self.parks = []
115|
116|    def way(self, w):
117|        pt = self._park_type(w.tags)
118|        if not pt:
119|            return
120|        coords = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
121|        if len(coords) < 3:
122|            return
123|        name = w.tags.get("name")
124|        self.parks.append(
125|            {"osm_id": w.id, "park_type": pt, "coords": coords, "name": name}
126|        )
127|
128|    def relation(self, r):
129|        pt = self._park_type(r.tags)
130|        if not pt:
131|            return
132|        # Extract outer members (simple approach: first outer way's coords)
133|        outer_coords = None
134|        for m in r.members:
135|            if m.role == "outer" and m.type == "w":
136|                try:
137|                    w = osmium._osmium.Way(m.ref)
138|                    coords = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
139|                    if len(coords) >= 3:
140|                        outer_coords = coords
141|                        break
142|                except:
143|                    pass
144|        if not outer_coords:
145|            return
146|        name = r.tags.get("name")
147|        self.parks.append(
148|            {"osm_id": r.id, "park_type": pt, "coords": outer_coords, "name": name}
149|        )
150|
151|    @staticmethod
152|    def _park_type(tags):
153|        for tag in tags:
154|            if tag.k in PARK_TAGS and tag.v in PARK_TAGS[tag.k]:
155|                return tag.v
156|        return None
157|
158|
159|def encode_coordinates(coords):
160|    if not coords:
161|        return b""
162|    buf = bytearray()
163|    prev_lon = round(coords[0][0] * 100_000)
164|    prev_lat = round(coords[0][1] * 100_000)
165|    for lon, lat in coords[1:]:
166|        cur_lon = round(lon * 100_000)
167|        cur_lat = round(lat * 100_000)
168|        buf.extend(encode_varint(zigzag_encode(cur_lon - prev_lon)))
169|        buf.extend(encode_varint(zigzag_encode(cur_lat - prev_lat)))
170|        prev_lon, prev_lat = cur_lon, cur_lat
171|    return bytes(buf)
172|
173|
174|def enc(feat, pid):
175|    buf = bytearray()
176|    buf.extend(encode_varint(zigzag_encode(feat["osm_id"] - pid)))
177|    n = len(feat["coords"])
178|    buf.append(n if n < 256 else 255)
179|    if n >= 256:
180|        buf.append(n & 0xFF)
181|        buf.append((n >> 8) & 0xFF)
182|    buf.extend(
183|        struct.pack(
184|            "<ii",
185|            round(feat["coords"][0][0] * 100000),
186|            round(feat["coords"][0][1] * 100000),
187|        )
188|    )
189|    buf.extend(encode_coordinates(feat["coords"]))
190|    # Park type as u8 string
191|    nb = feat["park_type"].encode("utf-8")
192|    buf.append(len(nb))
193|    buf.extend(nb)
194|    # Name
195|    flags = 0
196|    if feat.get("name"):
197|        flags |= 0x01
198|    buf.append(flags)
199|    if feat.get("name"):
200|        nb2 = feat["name"].encode("utf-8")
201|        buf.extend(struct.pack("<H", len(nb2)))
202|        buf.extend(nb2)
203|    return bytes(buf)
204|
205|
206|def build_state(abbr):
207|    pbfn = PBF_MAP.get(abbr)
208|    if not pbfn:
209|        return {"abbr": abbr, "error": "no mapping"}
210|    pbfp = PBF_DIR / f"{pbfn}-latest.osm.pbf"
211|    if not pbfp.exists():
212|        return {"abbr": abbr, "error": "no pbf"}
213|    s = get_state(abbr)
214|    t0 = time.time()
215|
216|    h = ParkHandler()
217|    h.apply_file(str(pbfp), locations=True)
218|    parks = h.parks
219|    if not parks:
220|        return {"abbr": abbr, "features": 0, "time_s": round(time.time() - t0, 1)}
221|
222|    per_cell = defaultdict(list)
223|    for p in parks:
224|        try:
225|            cell = h3.latlng_to_cell(p["coords"][0][1], p["coords"][0][0], H3_RES)
226|            p["cell"] = int(cell, 16) if isinstance(cell, str) else cell
227|            per_cell[p["cell"]].append(p)
228|        except:
229|            continue
230|    for c in per_cell:
231|        per_cell[c].sort(key=lambda p: p["osm_id"])
232|
233|    sc = sorted(per_cell.keys())
234|    mb = []
235|    pi = []
236|    bs = 8
237|    for i in range(0, len(sc), bs):
238|        bch = sc[i : i + bs]
239|        cr = []
240|        pd = []
241|        for cell in bch:
242|            rs = []
243|            pid = 0
244|            for p in per_cell[cell]:
245|                rec = enc(p, pid)
246|                pid = p["osm_id"]
247|                rs.append(rec)
248|            cr.append((cell, rs))
249|            pd.append((cell, len(rs)))
250|        if not cr:
251|            continue
252|        clat, clon = h3.cell_to_latlng(hex(bch[0])[2:])
253|        blk = encode_merged_block(cr, round(clon * 100000), round(clat * 100000))
254|        mb.append(blk)
255|        off = 0
256|        for cell, cnt in pd:
257|            pi.append(
258|                {
259|                    "h3_cell": cell,
260|                    "block_offset": 0,
261|                    "block_length": len(blk),
262|                    "feature_count": cnt,
263|                    "cell_index": off,
264|                    "min_lon": round(
265|                        min(p["coords"][0][0] for p in per_cell[cell]) * 100000
266|                    ),
267|                    "min_lat": round(
268|                        min(p["coords"][0][1] for p in per_cell[cell]) * 100000
269|                    ),
270|                    "max_lon": round(
271|                        max(p["coords"][0][0] for p in per_cell[cell]) * 100000
272|                    ),
273|                    "max_lat": round(
274|                        max(p["coords"][0][1] for p in per_cell[cell]) * 100000
275|                    ),
276|                }
277|            )
278|            off += 1
279|
280|    if not mb:
281|        return {"abbr": abbr, "features": 0}
282|    dd = b""
283|    cbs = [zstd.ZstdCompressor(level=1).compress(b) for b in mb]
284|    tf = sum(e["feature_count"] for e in pi)
285|    do = HEADER_SIZE
286|    dl = 0
287|    io = do
288|    il = 4 + len(pi) * INDEX_ENTRY_SIZE_V2
289|    bo = io + il
290|    ie = []
291|    for idx, pc_ in enumerate(pi):
292|        bi = idx // bs
293|        ie.append(
294|            {
295|                **pc_,
296|                "block_offset": bo + sum(len(cb) for cb in cbs[:bi]),
297|                "block_length": len(cbs[bi]) if bi < len(cbs) else 0,
298|            }
299|        )
300|    op = OUTPUT_DIR / f"{abbr}.parks.ptiles"
301|    with open(op, "wb") as f:
302|        write_header(
303|            f,
304|            MAGIC,
305|            VERSION,
306|            s.min_lat,
307|            s.min_lon,
308|            s.max_lat,
309|            s.max_lon,
310|            tf,
311|            len(mb),
312|            do,
313|            dl,
314|            io,
315|            il,
316|            bo,
317|        )
318|        f.write(struct.pack("<I", len(ie)))
319|        for e in ie:
320|            f.write(
321|                encode_index_entry_v2(
322|                    e["h3_cell"],
323|                    e["min_lon"],
324|                    e["min_lat"],
325|                    e["max_lon"],
326|                    e["max_lat"],
327|                    e["block_offset"],
328|                    e["block_length"],
329|                    e["feature_count"],
330|                    e["cell_index"],
331|                )
332|            )
333|        for cb in cbs:
334|            f.write(cb)
335|    sz = op.stat().st_size
336|    return {
337|        "abbr": abbr,
338|        "features": tf,
339|        "cells": len(per_cell),
340|        "bytes": sz,
341|        "time_s": round(time.time() - t0, 1),
342|    }
343|
344|
345|def main():
346|    import argparse
347|
348|    p = argparse.ArgumentParser()
349|    p.add_argument("--all", action="store_true")
350|    p.add_argument("--states")
351|    args = p.parse_args()
352|    targets = []
353|    if args.all:
354|        targets = [s.abbr for s in STATES]
355|    elif args.states:
356|        for a in args.states.split(","):
357|            s = get_state(a.strip())
358|            if s:
359|                targets.append(s.abbr)
360|    else:
361|        p.print_help()
362|        return
363|    for abbr in targets:
364|        try:
365|            r = build_state(abbr)
366|            if r and r.get("features"):
367|                print(
368|                    f"  {r['abbr']:2s} {r['features']:6d} parks {r['bytes']:10,d} B  {r['time_s']:6.1f}s",
369|                    flush=True,
370|                )
371|            elif r:
372|                print(f"  {r['abbr']:2s}  0", flush=True)
373|        except Exception as e:
374|            print(f"  ERROR {abbr}: {e}", flush=True)
375|            import traceback
376|
377|            traceback.print_exc()
378|
379|
380|if __name__ == "__main__":
381|    main()
382|