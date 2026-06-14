1|#!/usr/bin/env python3
2|"""
3|Build places.ptiles from per-state OSM PBF files.
4|Uses osmium FileProcessor + KeyFilter for fast node extraction.
5|Format: PTILESP v1, v2 merged-block.
6|"""
7|
8|import sys
9|
10|sys.stdout.reconfigure(line_buffering=True)
11|sys.path.insert(0, "/home/aoi/kino/projects/ptiles/scripts")
12|
13|import struct
14|import time
15|from pathlib import Path
16|from collections import defaultdict
17|
18|import h3
19|import zstandard as zstd
20|
21|from shared import (
22|    encode_varint,
23|    zigzag_encode,
24|    encode_index_entry_v2,
25|    INDEX_ENTRY_SIZE_V2,
26|    encode_merged_block,
27|    write_header,
28|    HEADER_SIZE,
29|)
30|from states import STATES, get_state
31|
32|OUTPUT_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
33|PBF_DIR = Path("/mnt/aoi/kino/ptiles/pbfs")
34|MAGIC = b"PTILESP\x00"
35|VERSION = 1
36|H3_RES = 7
37|
38|PBF_MAP = {
39|    "AL": "alabama",
40|    "AK": "alaska",
41|    "AZ": "arizona",
42|    "AR": "arkansas",
43|    "CA": "california",
44|    "CO": "colorado",
45|    "CT": "connecticut",
46|    "DE": "delaware",
47|    "DC": "district-of-columbia",
48|    "FL": "florida",
49|    "GA": "georgia",
50|    "HI": "hawaii",
51|    "ID": "idaho",
52|    "IL": "illinois",
53|    "IN": "indiana",
54|    "IA": "iowa",
55|    "KS": "kansas",
56|    "KY": "kentucky",
57|    "LA": "louisiana",
58|    "ME": "maine",
59|    "MD": "maryland",
60|    "MA": "massachusetts",
61|    "MI": "michigan",
62|    "MN": "minnesota",
63|    "MS": "mississippi",
64|    "MO": "missouri",
65|    "MT": "montana",
66|    "NE": "nebraska",
67|    "NV": "nevada",
68|    "NH": "new-hampshire",
69|    "NJ": "new-jersey",
70|    "NM": "new-mexico",
71|    "NY": "new-york",
72|    "NC": "north-carolina",
73|    "ND": "north-dakota",
74|    "OH": "ohio",
75|    "OK": "oklahoma",
76|    "OR": "oregon",
77|    "PA": "pennsylvania",
78|    "RI": "rhode-island",
79|    "SC": "south-carolina",
80|    "SD": "south-dakota",
81|    "TN": "tennessee",
82|    "TX": "texas",
83|    "UT": "utah",
84|    "VT": "vermont",
85|    "VA": "virginia",
86|    "WA": "washington",
87|    "WV": "west-virginia",
88|    "WI": "wisconsin",
89|    "WY": "wyoming",
90|}
91|
92|PT = [
93|    "city",
94|    "town",
95|    "village",
96|    "hamlet",
97|    "neighborhood",
98|    "suburb",
99|    "borough",
100|    "quarter",
101|    "isolated_dwelling",
102|]
103|PTI = {t: i for i, t in enumerate(PT)}
104|
105|
106|def extract(pbf):
107|    import osmium
108|
109|    fp = osmium.FileProcessor(pbf).with_filter(osmium.filter.KeyFilter("place"))
110|    out = []
111|    for obj in fp:
112|        lon = getattr(obj, "lon", None)
113|        if lon is None:
114|            continue
115|        pt = nm = None
116|        pop = 0
117|        an = None
118|        al = None
119|        for t in obj.tags:
120|            if t.k == "place" and t.v:
121|                pt = t.v
122|            elif t.k == "name" and t.v:
123|                nm = t.v
124|            elif t.k == "population" and t.v:
125|                try:
126|                    pop = int(t.v)
127|                except:
128|                    pass
129|            elif t.k == "alt_name" and t.v:
130|                an = t.v
131|            elif t.k == "admin_level" and t.v:
132|                try:
133|                    al = int(t.v)
134|                except:
135|                    pass
136|        if not pt or not nm:
137|            continue
138|        try:
139|            c = h3.latlng_to_cell(obj.lat, lon, 7)
140|        except:
141|            continue
142|        out.append(
143|            {
144|                "osm_id": obj.id,
145|                "lon": lon,
146|                "lat": obj.lat,
147|                "place_type": pt,
148|                "population": pop,
149|                "name": nm,
150|                "alt_name": an,
151|                "admin_level": al,
152|                "cell": int(c, 16) if isinstance(c, str) else c,
153|            }
154|        )
155|    return out
156|
157|
158|def enc(p, pid):
159|    b = bytearray()
160|    b.extend(encode_varint(zigzag_encode(p["osm_id"] - pid)))
161|    b.extend(struct.pack("<ii", round(p["lon"] * 100000), round(p["lat"] * 100000)))
162|    b.append(PTI.get(p["place_type"], 4))
163|    b.extend(encode_varint(p["population"]))
164|    nb = (p["name"] or "").encode("utf-8")
165|    b.extend(struct.pack("<H", len(nb)))
166|    b.extend(nb)
167|    f = 0
168|    if p.get("alt_name"):
169|        f |= 1
170|    if p.get("admin_level") is not None:
171|        f |= 2
172|    b.append(f)
173|    if p.get("alt_name"):
174|        an = p["alt_name"].encode("utf-8")
175|        b.extend(struct.pack("<H", len(an)))
176|        b.extend(an)
177|    if p.get("admin_level") is not None:
178|        b.append(p["admin_level"])
179|    return bytes(b)
180|
181|
182|def build_state(abbr):
183|    pbfn = PBF_MAP.get(abbr)
184|    if not pbfn:
185|        return {"abbr": abbr, "error": "no mapping"}
186|    pbfp = PBF_DIR / f"{pbfn}-latest.osm.pbf"
187|    if not pbfp.exists():
188|        return {"abbr": abbr, "error": "no pbf"}
189|    s = get_state(abbr)
190|    t0 = time.time()
191|    places = extract(str(pbfp))
192|    if not places:
193|        return {"abbr": abbr, "places": 0, "time_s": round(time.time() - t0, 1)}
194|    pc = defaultdict(list)
195|    for p in places:
196|        pc[p["cell"]].append(p)
197|    for c in pc:
198|        pc[c].sort(key=lambda p: p["osm_id"])
199|    sc = sorted(pc.keys())
200|    mb = []
201|    pi = []
202|    bs = 8
203|    for i in range(0, len(sc), bs):
204|        bch = sc[i : i + bs]
205|        cr = []
206|        pd = []
207|        for cell in bch:
208|            rs = []
209|            pid = 0
210|            for p in pc[cell]:
211|                rs.append(enc(p, pid))
212|                pid = p["osm_id"]
213|            cr.append((cell, rs))
214|            pd.append((cell, len(rs)))
215|        ch = hex(bch[0])[2:]
216|        cla, clo = h3.cell_to_latlng(ch)
217|        blk = encode_merged_block(cr, round(clo * 100000), round(cla * 100000))
218|        mb.append(blk)
219|        off = 0
220|        for cell, cnt in pd:
221|            pi.append(
222|                {
223|                    "h3_cell": cell,
224|                    "block_offset": 0,
225|                    "block_length": len(blk),
226|                    "feature_count": cnt,
227|                    "cell_index": off,
228|                    "min_lon": round(min(p["lon"] for p in pc[cell]) * 100000),
229|                    "min_lat": round(min(p["lat"] for p in pc[cell]) * 100000),
230|                    "max_lon": round(max(p["lon"] for p in pc[cell]) * 100000),
231|                    "max_lat": round(max(p["lat"] for p in pc[cell]) * 100000),
232|                }
233|            )
234|            off += 1
235|    dd = zstd.train_dictionary(512 * 1024, mb[:2000]).as_bytes()
236|    zd = zstd.ZstdCompressionDict(dd)
237|    cbs = [zstd.ZstdCompressor(level=12, dict_data=zd).compress(b) for b in mb]
238|    # Debug: check first compressed block magic
239|    if cbs:
240|        print(
241|            f"  First block: raw={len(mb[0])}B compressed={len(cbs[0])}B magic={cbs[0][:4].hex()}",
242|            flush=True,
243|        )
244|    tf = sum(e["feature_count"] for e in pi)
245|    do = HEADER_SIZE
246|    dl = len(dd)
247|    io = do + dl
248|    il = 4 + len(pi) * INDEX_ENTRY_SIZE_V2
249|    bo = io + il
250|    cur = bo
251|    ie = []
252|    # Map each per-cell index entry to its compressed block
253|    for idx, pc_ in enumerate(pi):
254|        block_idx = idx // bs
255|        ie.append(
256|            {
257|                **pc_,
258|                "block_offset": cur + sum(len(cb) for cb in cbs[:block_idx]),
259|                "block_length": len(cbs[block_idx]) if block_idx < len(cbs) else 0,
260|            }
261|        )
262|    # The actual blocks_offset should match where data starts in file
263|    # Recompute: blocks start after header + dict + actual index bytes
264|    actual_il = 4 + len(pi) * INDEX_ENTRY_SIZE_V2
265|    bo = io + actual_il
266|    # Recalculate block offsets from new bo
267|    cur = bo
268|    for idx, e in enumerate(ie):
269|        block_idx = idx // bs
270|        e["block_offset"] = cur + sum(len(cb) for cb in cbs[:block_idx])
271|    ie.sort(key=lambda e: e["h3_cell"])
272|    op = OUTPUT_DIR / f"{abbr}.places.ptiles"
273|    with open(op, "wb") as f:
274|        write_header(
275|            f,
276|            MAGIC,
277|            VERSION,
278|            s.min_lat,
279|            s.min_lon,
280|            s.max_lat,
281|            s.max_lon,
282|            tf,
283|            len(mb),
284|            do,
285|            dl,
286|            io,
287|            il,
288|            bo,
289|        )
290|        f.write(dd)
291|        f.write(struct.pack("<I", len(ie)))
292|        for e in ie:
293|            f.write(
294|                encode_index_entry_v2(
295|                    e["h3_cell"],
296|                    e["min_lon"],
297|                    e["min_lat"],
298|                    e["max_lon"],
299|                    e["max_lat"],
300|                    e["block_offset"],
301|                    e["block_length"],
302|                    e["feature_count"],
303|                    e["cell_index"],
304|                )
305|            )
306|        for cb in cbs:
307|            f.write(cb)
308|    return {
309|        "abbr": abbr,
310|        "places": tf,
311|        "cells": len(pc),
312|        "bytes": op.stat().st_size,
313|        "time_s": round(time.time() - t0, 1),
314|    }
315|
316|
317|def main():
318|    import argparse
319|
320|    p = argparse.ArgumentParser()
321|    p.add_argument("--all", action="store_true")
322|    p.add_argument("--states")
323|    args = p.parse_args()
324|    targets = []
325|    if args.all:
326|        targets = [s.abbr for s in STATES]
327|    elif args.states:
328|        for a in args.states.split(","):
329|            s = get_state(a.strip())
330|            if s:
331|                targets.append(s.abbr)
332|            else:
333|                print(f"Unknown: {a}")
334|    else:
335|        p.print_help()
336|        return
337|    for abbr in targets:
338|        try:
339|            r = build_state(abbr)
340|            if r and r.get("places"):
341|                print(
342|                    f"  {r['abbr']:2s} {r['places']:6d} places  {r['cells']:4d} cells  {r['bytes']:10,d} B  {r['time_s']:6.1f}s",
343|                    flush=True,
344|                )
345|            elif r:
346|                print(f"  {r['abbr']:2s}  0 places  ({r.get('error', '')})", flush=True)
347|        except Exception as e:
348|            print(f"  ERROR {abbr}: {e}", flush=True)
349|            import traceback
350|
351|            traceback.print_exc()
352|    print("\nDone")
353|
354|
355|if __name__ == "__main__":
356|    main()
357|