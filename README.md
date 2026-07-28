# PTILES — Binary Geospatial Format

## Demo

[![Watch the demo](https://img.youtube.com/vi/wG7tEsdkaCs/maxresdefault.jpg)](https://youtu.be/wG7tEsdkaCs)

**Live map:** **[https://steele.red/ptiles/](https://steele.red/ptiles/)** — click on any building in the US to see nearby businesses, toggle PTILES vector layers, search by business name.

_Every building in the United States—77 million footprints with business names and details extracted from OpenStreetMap. The source data comes from [Protomaps PMTiles](https://protomaps.com/), which is derived from OSM's global building dataset._

Binary format for GPS to feature lookup with full geometry. Per-file, per-layer, compressed.

## What it is

PTILES is a compact binary format for geospatial **feature lookup** — given a GPS coordinate, what building am I in, what road is nearest, what business is here. Each file covers one layer (buildings, roads, water, business, etc.) for a geographic region. Files are self-describing with a 256-byte header, zstd dictionary, spatial H3 index, and compressed data blocks.

**PTILES vs PMTiles:** PMTiles is a storage format for pre-rendered map tiles (MVT) designed for cheap self-hosted map rendering via HTTP range requests off S3. It answers "draw this 256x256 tile at zoom 14." PTILES is a feature database organized for spatial lookup — it answers "what's at this GPS coordinate." A PMTiles query at a lat/lon fetches a whole tile (~100KB+) and decodes every feature in it. A PTILES query hashes to one H3 cell, decompresses one small block (~1-5KB), and iterates only the buildings in that cell. PTILES files are also entirely offline-readable with no server.

Current format: **v8 for buildings** (77M footprints, ~4 bytes/building), **v2 for roads** (56M segments), **v1 for water/business/places/rail/parks/admin**.

## Client library

[JavaScript client](https://github.com/baocin/ptile-client) — read PTILES files in Node.js and the browser.

```js
import { definePtiles } from "ptile-client";
import * as h3 from "h3-js";

const { ptile, ready } = definePtiles({
  source: "https://maps.mydatatimeline.com/maps/",
  h3,
});
await ready;
const building = await ptile(36.16, -86.78);
```

## PTILES vs Parquet

For lat/lng feature lookups (your use case), PTILES beats Parquet at every level. Parquet is designed for columnar analytics (aggregations, scans, ad-hoc SQL). PTILES is designed for spatial point queries (single-feature lookup at a coordinate).

### The key difference: query cost

A PTILES query reads exactly 1 H3 cell's data. A Parquet query reads row groups filtered by column statistics — which is still way more data.

|                           | Parquet (business_v1, 2.7 GB)                                                                            | PTILES (51 state files, 5 GB total)    |
| ------------------------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| **Query pattern**         | Full column scan or row-group filter pushdown                                                            | O(1) H3 hash → decompress 1 block      |
| **Data read per query**   | Scans ~2 GB (lat/lon columns) or reads 50-80 row groups (~5-10 MB each with stats pushdown)              | ~1-5 KB (1 index entry + 1 ZSTD block) |
| **Row group stats help?** | Partially. Each 524K-row group spans half the country in lat, so a narrow bbox still touches most groups | N/A — no row groups, just H3 cells     |
| **Browser serving**       | Needs DuckDB WASM (8 MB download, heavy init, ~500ms startup)                                            | ~5 KB JS decoder, immediate queries    |
| **Range requests**        | 50-80 per query (reading matching row groups)                                                            | 1 per query (single block)             |
| **Server-side query**     | Fast once loaded (DuckDB ~50ms for warm bbox query)                                                      | Sub-millisecond (in-memory index)      |
| **Offline capable**       | Only with DuckDB engine                                                                                  | Yes, pure binary decode                |
| **Analytics**             | Excellent (count, group by, join)                                                                        | Poor (must scan all blocks)            |

### Can Parquet be optimized?

Yes — three ways, but none change the browser-serving calculus:

1. **Sort by H3 cell + tiny row groups.** Add an `h3_cell` column, sort by it, write with `row_group_size=16384` instead of 524288. Then `WHERE h3_cell = <query_cell>` eliminates 99.9% of row groups from stats. Still needs DuckDB WASM in the browser.

2. **Partition by state.** Already have `state_abbr`. Use Hive-style partitioning: `business/state=TN/data.parquet`. Then state-level queries only scan one directory.

3. **Both.** Partition by state, sort within by H3 cell. Best server-side option.

### When to pick each

| You want Parquet when...                      | You want PTILES when...                  |
| --------------------------------------------- | ---------------------------------------- |
| "Count how many restaurants have websites"    | "What businesses are at this lat/lng?"   |
| Server-side DuckDB with fast local disk       | Static HTTP serving to browsers from S3  |
| Ad-hoc SQL across the full dataset            | Fixed query pattern (point → features)   |
| You already have DuckDB loaded for other work | You want a lightweight in-browser viewer |
| Aggregations, joins, histograms               | Spatial lookups, per-cell iteration      |

### The national business layer numbers

|                        | Parquet (existing)                 | PTILES (51 state files)        |
| ---------------------- | ---------------------------------- | ------------------------------ |
| Rows                   | 41.8M POIs                         | 41.8M POIs (same data)         |
| File size              | 2.7 GB (single file)               | 5 GB (51 files, 52 MB avg)     |
| H3 cells per row group | 15K+ per 524K rows                 | 1 per block                    |
| Row groups             | 80                                 | N/A — 18K+ H3 blocks per state |
| Schema                 | 13 cols (full Foursquare+Overture) | Full records with brand/chain  |
| State filter           | Needs partition or scan            | Natural (one file per state)   |

The parquet file is kept alongside PTILES for server-side analytics: `duckdb -c "SELECT category, count(*) FROM 'business_v1.parquet' GROUP BY category"` is a natural SQL query that PTILES can't do efficiently. For the browser lookup tool, PTILES is the right format.

## Compression evolution

| Version | What changed                                                                                                                                               | Per-building savings |
| ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| v1      | Raw coordinates (i32), inline strings                                                                                                                      | ~80 bytes/building   |
| v6      | Delta OSM IDs, indexed building types, zigzag varint coords                                                                                                | ~15 bytes/building   |
| **v7**  | **Wall segment encoding** — 2 bytes per wall (angle+distance with step quantization) instead of full coordinate deltas                                     | ~10 bytes/building   |
| **v8**  | **String table + per-cell string dedup**, cell-relative i16 first vertex, optional fields in flags2 byte (name, category, name_source, poi_osm_id, height) | ~4 bytes/building    |
| **v9**  | **OSM business tags** — shop, amenity, opening_hours packed into flags2 bits (0x20, 0x40); no schema change, v8-compatible decode                          | +0 bytes/building    |

v8 builds on v7's wall encoding but adds per-block string deduplication: building types, names, categories, and name sources are stored once in a string table at the start of each block, referenced by 1-byte index.

v9 adds OSM business tags to the building record without increasing size — shop and amenity use flags2 bits (0x20, 0x40), opening_hours is a table ref like name. Backward-compatible with v8 decoders.

## Current file sizes

|| Layer | Files | Format | Total size | Features | Bytes/feature |
|| --------- | ------------ | ------ | --------------- | ---------- | --------------------- |
|| Buildings | 51 per-state | v9 | ~1.1 GB | 77M | ~15 avg, ~4 with name |
|| Roads | 13 per-state | v2 | ~700 MB | 9M | ~78 |
|| Water | 51 per-state | v1 | ~1.2 GB | 12M | ~100 |
|| Business | 51 per-state | v4 | ~2.0 GB | 7.9M POIs | ~253 |
|| Address | 51 per-state | v1 | ~120 MB | 1.2M | ~100 |
|| Places | 51 per-state | v1 | ~15 MB | 50K | ~300 |
|| Rail | 51 per-state | v1 | ~448 KB | 10K | ~45 |
|| Parks | 51 per-state | v1 | ~27 MB | 200K | ~135 |
|| Admin | 1 US-wide | v1 | ~31 MB | grid cells | variable |
|| Camera | 1 US-wide | v1 | 3.5 MB | 129K | ~27 |
|| Signals | 1 US-wide | v1 | 21 MB | 2.1M | ~10 |
|| **Total** | | | **~5.2 GB** | | |


## Format evolution milestones

| Milestone   | Date    | What landed                                                                                                                                                                                            |
| ----------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| v1          | 2025 Q4 | Initial format: absolute coordinates, inline strings, 19-byte spatial index                                                                                                                            |
| v6          | 2026 Q1 | Delta encoding (OSM IDs, coordinates), indexed building types, zstd dict. 99.1% compression vs PMTiles source.                                                                                         |
| v7          | 2026 Q1 | Wall segment encoding: wall vertices reduced to 2 packed bytes. First/last vertex absolute, intermediate as (angle, distance) pairs with 0.2m step quantization.                                       |
| v8          | 2026 Q2 | Per-block string table (building types, names, categories deduplicated). Cell-relative i16 first vertex (vs absolute microdegree). Optional metadata in flags2 byte. National US build pipeline added. |
| v2 index    | 2026 Q2 | 37-byte index entries with per-cell bounding box (microdegree). Enables spatial pruning before decompression. Merged blocks: multiple cells per zstd frame.                                            |
| Multi-layer | 2026 Q2 | Format generalized beyond buildings: roads (v2), water (v1), business (v2), places (v1), rail (v1), parks (v1), admin (v1). Each layer has its own magic byte.                                         |

## File structure

```
  Header (256 bytes) — magic, version, bbox, feature count,
                       offsets for dict/index/blocks
  Zstd Dictionary (optional, ~512 KB typical)
  Spatial Index: sorted H3 res 7 cell to (block_offset,
                  block_length, feature_count)
    v1: 19-byte entries (compact: 6B offset, 3B length, 2B count)
    v2: 37-byte entries (adds per-cell bbox in microdegrees,
         cell_index_in_block for merged blocks)
  Data Blocks (zstd compressed, v2 = merged: multiple cells
               per block for better compression ratio)
    v8 buildings: each block starts with string table, then
                  u32-prefixed records with cell-relative i16
                  first vertex + zigzag varint deltas
```

## Header (256 bytes)

| Offset | Size | Type   | Field         | Description                                                             |
| ------ | ---- | ------ | ------------- | ----------------------------------------------------------------------- |
| 0      | 8    | bytes  | magic         | `PTILESF\0` (buildings), `PTILESR\0` (roads), `PTILESA\0` (admin), etc. |
| 8      | 1    | uint8  | version       | Format version for this layer                                           |
| 9      | 3    | -      | reserved      | Padding for alignment                                                   |
| 12     | 4    | float  | min_lat       | Bounding box south                                                      |
| 16     | 4    | float  | min_lon       | Bounding box west                                                       |
| 20     | 4    | float  | max_lat       | Bounding box north                                                      |
| 24     | 4    | float  | max_lon       | Bounding box east                                                       |
| 28     | 8    | uint64 | feature_count | Total features in file                                                  |
| 36     | 4    | uint32 | block_count   | Number of compressed blocks                                             |
| 40     | 8    | uint64 | dict_offset   | Byte offset to zstd dictionary                                          |
| 48     | 4    | uint32 | dict_length   | Size of dictionary (0 if none)                                          |
| 52     | 8    | uint64 | index_offset  | Byte offset to spatial index                                            |
| 60     | 4    | uint32 | index_length  | Size of index section                                                   |
| 64     | 8    | uint64 | blocks_offset | Byte offset to first data block                                         |
| 72     | 8    | uint64 | aux_offset    | Auxiliary data offset (0 if none)                                       |
| 80     | 4    | uint32 | aux_length    | Auxiliary data length                                                   |
| 84     | 8    | uint64 | created_at    | Unix timestamp (seconds)                                                |
| 92     | 4    | -      | reserved      |                                                                         |
| 96     | 4    | uint32 | data_version  | Pipeline/data build version                                             |
| 100    | 156  | -      | reserved      | Future use                                                              |

## Magic bytes

| Byte   | ASCII | Layer                      |
| ------ | ----- | -------------------------- |
| `0x46` | `F`   | Buildings (footprints)     |
| `0x52` | `R`   | Roads                      |
| `0x41` | `A`   | Admin boundaries           |
| `0x57` | `W`   | Water                      |
| `0x50` | `P`   | Places                     |
| `0x4E` | `N`   | Parks                      |
| `0x54` | `T`   | Rail/transit               |
| `0x49` | `I`   | POIs                       |
| `0x44` | `D`   | Address ranges             |
| `0x55` | `U`   | Routing (companion format) |

## v8 Building record format

After the block's string table, each building is a variable-length record:

```
u32     record_length     (bytes, excluding this field)
varint  osm_id_delta      (zigzag delta from previous OSM ID)
u8      flags             (bits 0-1: use_class, 2-3: height_tier, 4-7: vc_packed)
[ u8    vertex_raw ]      (only if vc_packed == 0x0F)
i16     first_lon         (cell-relative microdegrees: center.lon * 100000 + offset)
i16     first_lat         (cell-relative microdegrees)
[ varint delta_lon/pairs ] (zigzag deltas from prev vertex, times vertex_count-1)
u8      btype_idx         (index into string table; 0xFF = inline follows)
[ u8_len + UTF-8 ]        (inline building type, only if btype_idx == 0xFF)
u8      flags2            (extended flags)
  u8    name_ref          (if flags2 & 0x01)
  u8    category_ref      (if flags2 & 0x02)
  u8    name_source_ref   (if flags2 & 0x04)
  u64   poi_osm_id        (if flags2 & 0x08)
  u8    height_raw        (if flags2 & 0x10, 0.5m steps)
```

Centroid is computed from coordinate mean (not stored).

## Hosted tiles

All 51 states + DC files available at:

```
https://maps.mydatatimeline.com/maps/v4-20260711/{ST}.{layer}.ptiles
```

v4 build (2026-07-11) replaces all prior tile sets. Layers:

| Layer                   | Format | Source                     |
| ----------------------- | ------ | -------------------------- |
| buildings_v9            | v9     | OSM PBF                    |
| business_v4             | v4     | Overture Maps places       |
| highways_v2             | v2     | OSM highways               |
| business_name_index     | v1     | Business name search index |
| address_v1              | v1     | OSM address points         |
| water_v1                | v1     | OSM water features         |
| places_v1               | v1     | OSM places                 |
| parks_v1                | v1     | OSM parks                  |
| rail_v1                 | v1     | OSM rail lines             |
| roads/{ST}.roads.ptiles | v2     | OSM roads                  |

## Brand icons

Favicons for the business/brand layer, hosted alongside the tiles:

```
https://maps.mydatatimeline.com/brand-favicons/{md5}.png
```

`{md5}` is the lowercase hex MD5 of the **registered domain** (second level +
TLD, no scheme, no `www.`, no path), and the extension is always `.png`:

```python
import hashlib
name = hashlib.md5("allstarmovers253.com".encode()).hexdigest() + ".png"
# 7a114db59dd4ebacd26d18c9739c1200.png
```

Hashing the domain rather than using it directly keeps dots and slashes out of
object keys. Mapping a brand back to its domain is the consumer's job — the
tiles reference brands by name, not by icon URL.

### Coverage is partial — handle the miss

The bucket holds **77,977 objects, ~96 MB** (uploaded 2026-07-26). That is a
small fraction of the brand list, and the two do not line up: sampling 40
domains from `brand_domains_clean.csv` found 2% present, and 40 rows from
`brand_favicon_lookup.csv` found 5%. Sherwin-Williams, for one, is absent.

So treat a missing icon as the normal case, not an error, and **check the HTTP
status**. A miss returns Cloudflare's error page — 404 with ~27 KB of
`text/html` — so a consumer that trusts the response body or its size will
happily render an HTML page as an image.

The downloaded-but-not-uploaded remainder lives on NFS; see
[`scripts/BRAND_FAVICONS.md`](scripts/BRAND_FAVICONS.md) for the pipeline, the
two sources (Google s2, DDG ip3) and the rsync/rclone upload steps.

### Where the brand names are codified

Not in this repo — these are generated artifacts on NFS under
`/mnt/core/kino/ptiles/data/`, and are not versioned:

| File | Rows | What |
| --- | --- | --- |
| `brand_domains_clean.csv` | 92K | brand → domain, deduped and junk-filtered. The one to use. |
| `brand_domains.csv` | 109K | raw scrape, keeps quirks |
| `brand_favicon_lookup.csv` | 25K | brand → `{md5}.png` → domain, i.e. the join already done |
| `brand_store_urls.parquet` | 950K | store-specific URLs, range-queryable over HTTP |
| `brand_url_dump.csv` | 1.38M | full dump; its fallback column is wrong |

`BRAND_DOMAINS.md` in that directory documents how they are derived: a scan of
`parquet/v2/*/business_v2.parquet` across 51 states for `brand` and `website`,
collapsed per brand, then normalised and checked for near-duplicate brand names
with trigram-indexed Levenshtein.

Quality caveat: the mapping is scraped from whatever OSM POIs carry, so a brand
can pick up an unrelated domain. `brand_favicon_lookup.csv` maps Starbucks to
`allstarmovers253.com`. Do not treat brand → domain as authoritative.

## Building

Build scripts in `scripts/`:

```bash
# Single state buildings (v8)
uv run --with osmium --with h3 --with zstandard --with shapely --with numpy \
    python scripts/build_state_v8.py TN

# All 51 states
uv run --with osmium --with h3 --with zstandard --with shapely --with numpy \
    python scripts/build_state_v8.py --all

# Roads
uv run --with osmium --with h3 --with zstandard --with shapely \
    python scripts/build_roads.py data/pbfs/tennessee-latest.osm.pbf data/states/TN.roads.ptiles

# Water, business, admin — see scripts/ for each
```

## License

MIT — format spec and build scripts.
Building data derived from OpenStreetMap (ODbL) and Overture Maps (Community Dataset Agreement).

## Direct Downloads (v4 — 2026-07-11)

All 51 states + DC, 10 layers, 5.2 GB total.

### Buildings v9

```
maps.mydatatimeline.com/maps/v4-20260711/{ST}.buildings_v9.ptiles
```

77M+ footprints with OSM tags (shop, amenity, opening_hours), H3 resolution 7.

### Business v4

```
maps.mydatatimeline.com/maps/v4-20260711/{ST}.business_v4.ptiles
```

Overture Maps places, sequential IDs, cell-relative coordinates, 7.9M records.

### Highways v2

```
maps.mydatatimeline.com/maps/v4-20260711/{ST}.highways_v2.ptiles
```

OSM highway ways as drivable segments, H3 resolution 7.

### Business Name Index

```
maps.mydatatimeline.com/maps/v4-20260711/{ST}.business_name_index.ptiles
```

Alphabetical prefix index for business name search, 1 block per letter prefix.

### All layers download

```bash
BASE="https://maps.mydatatimeline.com/maps/v4-20260711"
for ST in AL AK AZ AR CA CO CT DC DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY; do
  for LAYER in buildings_v9 business_v4 highways_v2 business_name_index address_v1 water_v1 places_v1 parks_v1 rail_v1; do
    curl -O "$BASE/$ST.$LAYER.ptiles"
  done
  curl -O "$BASE/roads/$ST.roads.ptiles"
done
```
