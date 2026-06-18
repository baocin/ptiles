1|# PTILES — Binary Geospatial Format
2|
3|## Demo
4|
5|[![Watch the demo](https://img.youtube.com/vi/wG7tEsdkaCs/maxresdefault.jpg)](https://youtu.be/wG7tEsdkaCs)
6|
7|_Every building in the United States—77 million footprints with business names and details extracted from OpenStreetMap. The source data comes from [Protomaps PMTiles](https://protomaps.com/), which is derived from OSM's global building dataset._
8|
9|Binary format for GPS → feature lookup with full geometry. Per-file, per-layer, compressed.
10|
11|## What it is
12|
PTILES is a compact binary format for geospatial **feature lookup** — given a GPS coordinate, what building am I in, what road is nearest, what business is here. Each file covers one layer (buildings, roads, water, business, etc.) for a geographic region. Files are self-describing with a 256-byte header, zstd dictionary, spatial H3 index, and compressed data blocks.

**PTILES vs PMTiles:** PMTiles is a storage format for pre-rendered map tiles (MVT) designed for cheap self-hosted map rendering via HTTP range requests off S3. It answers "draw this 256x256 tile at zoom 14." PTILES is a feature database organized for spatial lookup — it answers "what's at this GPS coordinate." A PMTiles query at a lat/lon fetches a whole tile (~100KB+) and decodes every feature in it. A PTILES query hashes to one H3 cell, decompresses one small block (~1-5KB), and iterates only the buildings in that cell. PTILES files are also entirely offline-readable with no server.

Current format: **v8 for buildings** (77M footprints, ~4 bytes/building), **v2 for roads** (56M segments), **v1 for water/business/places/rail/parks/admin**.
16|
17|## Client library
18|
19|[JavaScript client](https://github.com/baocin/ptile-client) — read PTILES files in Node.js and the browser.
20|
21|`js
22|import { definePtiles } from "ptile-client";
23|import * as h3 from "h3-js";
24|
25|const { ptile, ready } = definePtiles({
26|  source: "https://maps.mydatatimeline.com/maps/",
27|  h3,
28|});
29|await ready;
30|const building = await ptile(36.16, -86.78);
31|`
32|
33|## Compression evolution
34|
35|| Version | What changed | Per-building savings |
36|| ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
37|| v1 | Raw coordinates (i32), inline strings | ~80 bytes/building |
38|| v6 | Delta OSM IDs, indexed building types, zigzag varint coords | ~15 bytes/building |
39|| **v7** | **Wall segment encoding** — 2 bytes per wall (angle+distance with step quantization) instead of full coordinate deltas. First/last vertex absolute, intermediate walls as packed `(angle_step, distance_step)` pairs. | ~10 bytes/building |
40|| **v8** | **String table + per-cell string dedup**, cell-relative i16 first vertex (instead of full microdegree), optional fields in flags2 byte (name, category, name_source, poi_osm_id, height) | ~4 bytes/building |
41|
42|v8 builds on v7's wall encoding but adds per-block string deduplication: building types, names, categories, and name sources are stored once in a string table at the start of each block, referenced by 1-byte index. This eliminates repeated strings like "residential" that made up 60% of the per-record overhead.
43|
44|## Current file sizes
45|
46|| Layer | Files | Format | Total size | Features | Bytes/feature |
47|| --------- | ------------ | ------ | ---------- | ---------- | --------------------- |
48|| Buildings | 51 per-state | v8 | ~1.1 GB | 77M | ~15 avg, ~4 with name |
49|| Roads | 51 per-state | v2 | ~1.5 GB | 56M | ~28 |
50|| Water | 51 per-state | v1 | ~100 MB | 12M | ~8 |
51|| Business | 51 per-state | v2 | ~975 MB | 75M POIs | ~13 |
52|| Places | 51 per-state | v1 | ~15 MB | 50K | ~300 |
53|| Rail | 51 per-state | v1 | ~448 KB | 10K | ~45 |
54|| Parks | 51 per-state | v1 | ~27 MB | 200K | ~135 |
55|| Admin | 1 US-wide | v1 | ~31 MB | grid cells | variable |
56|
57|**Total: ~3.8 GB for the full US, all layers.**
58|
59|## Format evolution milestones
60|
61|| Milestone | Date | What landed |
62|| ----------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
63|| v1 | 2025 Q4 | Initial format: absolute coordinates, inline strings, 19-byte spatial index |
64|| v6 | 2026 Q1 | Delta encoding (OSM IDs, coordinates), indexed building types, zstd dict. 99.1% compression vs PMTiles source. |
65|| v7 | 2026 Q1 | Wall segment encoding: wall vertices reduced to 2 packed bytes. First/last vertex absolute, intermediate as (angle, distance) pairs with 0.2m step quantization. |
66|| v8 | 2026 Q2 | Per-block string table (building types, names, categories deduplicated). Cell-relative i16 first vertex (vs absolute microdegree). Optional metadata in flags2 byte. National US build pipeline added. |
67|| v2 index | 2026 Q2 | 37-byte index entries with per-cell bounding box (microdegree). Enables spatial pruning before decompression. Merged blocks: multiple cells per zstd frame. |
68|| Multi-layer | 2026 Q2 | Format generalized beyond buildings: roads (v2), water (v1), business (v2), places (v1), rail (v1), parks (v1), admin (v1). Each layer has its own magic byte. |
69|
70|## File structure
71|
72|`
73|┌──────────────────────────────────────────────────────────────────┐
74|│ Header (256 bytes) — magic, version, bbox, feature count,       │
75|│                     offsets for dict/index/blocks                │
76|├──────────────────────────────────────────────────────────────────┤
77|│ Zstd Dictionary (optional, ~512 KB typical)                     │
78|├──────────────────────────────────────────────────────────────────┤
79|│ Spatial Index: sorted H3 res 7 cell → (block_offset,           │
80|│                 block_length, feature_count)                     │
81|│   v1: 19-byte entries (compact: 6B offset, 3B length, 2B count) │
82|│   v2: 37-byte entries (adds per-cell bbox in microdegrees,      │
83|│        cell_index_in_block for merged blocks)                   │
84|├──────────────────────────────────────────────────────────────────┤
85|│ Data Blocks (zstd compressed, v2 = merged: multiple cells       │
86|│             per block for better compression ratio)             │
87|│   v8 buildings: each block starts with string table, then       │
88|│                 u32-prefixed records with cell-relative i16      │
89|│                 first vertex + zigzag varint deltas              │
90|└──────────────────────────────────────────────────────────────────┘
91|`
92|
93|## Header (256 bytes)
94|
95|| Offset | Size | Type | Field | Description |
96|| ------ | ---- | ------ | ------------- | ----------------------------------------------------------------------- |
97|| 0 | 8 | bytes | magic | `PTILESF\0` (buildings), `PTILESR\0` (roads), `PTILESA\0` (admin), etc. |
98|| 8 | 1 | uint8 | version | Format version for this layer |
99|| 9 | 3 | - | reserved | Padding for alignment |
100|| 12 | 4 | float | min_lat | Bounding box south |
101|| 16 | 4 | float | min_lon | Bounding box west |
102|| 20 | 4 | float | max_lat | Bounding box north |
103|| 24 | 4 | float | max_lon | Bounding box east |
104|| 28 | 8 | uint64 | feature_count | Total features in file |
105|| 36 | 4 | uint32 | block_count | Number of compressed blocks |
106|| 40 | 8 | uint64 | dict_offset | Byte offset to zstd dictionary |
107|| 48 | 4 | uint32 | dict_length | Size of dictionary (0 if none) |
108|| 52 | 8 | uint64 | index_offset | Byte offset to spatial index |
109|| 60 | 4 | uint32 | index_length | Size of index section |
110|| 64 | 8 | uint64 | blocks_offset | Byte offset to first data block |
111|| 72 | 8 | uint64 | aux_offset | Auxiliary data offset (0 if none) |
112|| 80 | 4 | uint32 | aux_length | Auxiliary data length |
113|| 84 | 8 | uint64 | created_at | Unix timestamp (seconds) |
114|| 92 | 4 | - | reserved | |
115|| 96 | 4 | uint32 | data_version | Pipeline/data build version |
116|| 100 | 156 | - | reserved | Future use |
117|
118|## Magic bytes
119|
120|| Byte | ASCII | Layer |
121|| ------ | ----- | -------------------------- |
122|| `0x46` | `F` | Buildings (footprints) |
123|| `0x52` | `R` | Roads |
124|| `0x41` | `A` | Admin boundaries |
125|| `0x57` | `W` | Water |
126|| `0x50` | `P` | Places |
127|| `0x4E` | `N` | Parks |
128|| `0x54` | `T` | Rail/transit |
129|| `0x49` | `I` | POIs |
130|| `0x44` | `D` | Address ranges |
131|| `0x55` | `U` | Routing (companion format) |
132|
133|## v8 Building record format
134|
135|After the block's string table, each building is a variable-length record:
136|
137|`
138|u32     record_length     (bytes, excluding this field)
139|varint  osm_id_delta      (zigzag delta from previous OSM ID)
140|u8      flags             (bits 0-1: use_class, 2-3: height_tier, 4-7: vc_packed)
141|[ u8    vertex_raw ]      (only if vc_packed == 0x0F)
142|i16     first_lon         (cell-relative microdegrees: center.lon * 100000 + offset)
143|i16     first_lat         (cell-relative microdegrees)
144|[ varint delta_lon/pairs ] (zigzag deltas from prev vertex, × vertex_count-1)
145|u8      btype_idx         (index into string table; 0xFF = inline follows)
146|[ u8_len + UTF-8 ]        (inline building type, only if btype_idx == 0xFF)
147|u8      flags2            (extended flags)
148|  u8    name_ref          (if flags2 & 0x01)
149|  u8    category_ref      (if flags2 & 0x02)
150|  u8    name_source_ref   (if flags2 & 0x04)
151|  u64   poi_osm_id        (if flags2 & 0x08)
152|  u8    height_raw        (if flags2 & 0x10, 0.5m steps)
153|`
154|
155|Centroid is computed from coordinate mean (not stored).
156|
157|## Hosted tiles
158|
159|All 51 states + DC building files available at:
160|
161|`
162|https://maps.mydatatimeline.com/maps/{ABBR}.buildings_v8.ptiles
163|`
164|
165|Roads, water, business, places, rail, parks, and admin layers also hosted at the same base URL.
166|
167|## Building
168|
169|Build scripts in `scripts/`:
170|
171|`bash
172|# Single state buildings (v8)
173|uv run --with osmium --with h3 --with zstandard --with shapely --with numpy \
174|    python scripts/build_state_v8.py TN
175|
176|# All 51 states
177|uv run --with osmium --with h3 --with zstandard --with shapely --with numpy \
178|    python scripts/build_state_v8.py --all
179|
180|# Roads
181|uv run --with osmium --with h3 --with zstandard --with shapely \
182|    python scripts/build_roads.py data/pbfs/tennessee-latest.osm.pbf data/states/TN.roads.ptiles
183|
184|# Water, business, admin — see scripts/ for each
185|`
186|
187|## License
188|
189|MIT — format spec and build scripts.
190|Building data derived from OpenStreetMap (ODbL) and Overture Maps (Community Dataset Agreement).
191|
