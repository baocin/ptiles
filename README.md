# PTILES — Binary Geospatial Format

## Demo

[![Watch the demo](https://img.youtube.com/vi/wG7tEsdkaCs/maxresdefault.jpg)](https://youtu.be/wG7tEsdkaCs)

_Every building in the United States—77 million footprints with business names and details extracted from OpenStreetMap. The source data comes from [Protomaps PMTiles](https://protomaps.com/), which is derived from OSM's global building dataset._

Binary format for GPS → feature lookup with full geometry. Per-file, per-layer, compressed.

## What it is

PTILES is a compact binary tile format for geospatial features. Each file covers one layer (buildings, roads, water, business, etc.) for a geographic region. Files are self-describing with a 256-byte header, zstd dictionary, spatial H3 index, and compressed data blocks.

Current format: **v8 for buildings** (77M footprints, ~4 bytes/building), **v2 for roads** (56M segments), **v1 for water/business/places/rail/parks/admin**.

## Compression evolution

| Version | What changed                                                                                                                                                                                                          | Per-building savings |
| ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| v1      | Raw coordinates (i32), inline strings                                                                                                                                                                                 | ~80 bytes/building   |
| v6      | Delta OSM IDs, indexed building types, zigzag varint coords                                                                                                                                                           | ~15 bytes/building   |
| **v7**  | **Wall segment encoding** — 2 bytes per wall (angle+distance with step quantization) instead of full coordinate deltas. First/last vertex absolute, intermediate walls as packed `(angle_step, distance_step)` pairs. | ~10 bytes/building   |
| **v8**  | **String table + per-cell string dedup**, cell-relative i16 first vertex (instead of full microdegree), optional fields in flags2 byte (name, category, name_source, poi_osm_id, height)                              | ~4 bytes/building    |

v8 builds on v7's wall encoding but adds per-block string deduplication: building types, names, categories, and name sources are stored once in a string table at the start of each block, referenced by 1-byte index. This eliminates repeated strings like "residential" that made up 60% of the per-record overhead.

## Current file sizes

| Layer     | Files        | Format | Total size | Features   | Bytes/feature         |
| --------- | ------------ | ------ | ---------- | ---------- | --------------------- |
| Buildings | 51 per-state | v8     | ~1.1 GB    | 77M        | ~15 avg, ~4 with name |
| Roads     | 51 per-state | v2     | ~1.5 GB    | 56M        | ~28                   |
| Water     | 51 per-state | v1     | ~100 MB    | 12M        | ~8                    |
| Business  | 51 per-state | v2     | ~975 MB    | 75M POIs   | ~13                   |
| Places    | 51 per-state | v1     | ~15 MB     | 50K        | ~300                  |
| Rail      | 51 per-state | v1     | ~448 KB    | 10K        | ~45                   |
| Parks     | 51 per-state | v1     | ~27 MB     | 200K       | ~135                  |
| Admin     | 1 US-wide    | v1     | ~31 MB     | grid cells | variable              |

**Total: ~3.8 GB for the full US, all layers.**

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
┌──────────────────────────────────────────────────────────────────┐
│ Header (256 bytes) — magic, version, bbox, feature count,       │
│                     offsets for dict/index/blocks                │
├──────────────────────────────────────────────────────────────────┤
│ Zstd Dictionary (optional, ~512 KB typical)                     │
├──────────────────────────────────────────────────────────────────┤
│ Spatial Index: sorted H3 res 7 cell → (block_offset,           │
│                 block_length, feature_count)                     │
│   v1: 19-byte entries (compact: 6B offset, 3B length, 2B count) │
│   v2: 37-byte entries (adds per-cell bbox in microdegrees,      │
│        cell_index_in_block for merged blocks)                   │
├──────────────────────────────────────────────────────────────────┤
│ Data Blocks (zstd compressed, v2 = merged: multiple cells       │
│             per block for better compression ratio)             │
│   v8 buildings: each block starts with string table, then       │
│                 u32-prefixed records with cell-relative i16      │
│                 first vertex + zigzag varint deltas              │
└──────────────────────────────────────────────────────────────────┘
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

## Client library

[JavaScript client](https://github.com/baocin/ptile-client) — read PTILES files in Node.js and the browser.

```js
import { definePtiles } from "ptile-client";
import * as h3 from "h3-js";

const { ptile, ready } = definePtiles({
  source: "https://pub-e46b7d7ee876916fd2db17000245b340.r2.dev/maps/",
  h3,
});
await ready;
const building = await ptile(36.16, -86.78);
```

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
[ varint delta_lon/pairs ] (zigzag deltas from prev vertex, × vertex_count-1)
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

All 51 states + DC building files available at:

```
https://pub-e46b7d7ee876916fd2db17000245b340.r2.dev/maps/{ABBR}.buildings_v8.ptiles
```

Roads, water, business, places, rail, parks, and admin layers also hosted at the same base URL.

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
