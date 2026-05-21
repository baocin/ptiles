# PTILES Client Library Requirements

Implementation spec for the three PTILES client libraries: TypeScript/JS
(`@ptiles/client`), Python (`ptiles`), and Rust (upgrade of existing
`ptiles` crate). All clients consume the binary `.ptiles` files at
`~/kino/projects/ptiles/data/states/*.ptiles` and `data/US.highways.ptiles`.

The Rust crate at `~/kino/projects/timeline/ptiles/src/` is the reference
implementation. The Python and TS clients MUST mirror the Rust data model
(field names, units, types) one-to-one so that records can round-trip
across language boundaries without translation.

---

## 1. Shared Binary Format Recap

The format details below are normative for all client implementations.
File layout, codec, and per-layer record schemas are duplicated here from
`scripts/shared.py` and `src/header.rs` / `src/codec.rs` / `src/index.rs`
to be a single source of truth for new client work.

### 1.1 File Layout

```
+--------------------------+ 0
| Header (256 bytes)       |
+--------------------------+ dict_offset
| Zstd dictionary          |
+--------------------------+ index_offset
| Spatial index            |
+--------------------------+ blocks_offset
| Compressed data blocks   |
| (one per H3 res-7 cell)  |
+--------------------------+ aux_offset
| Aux section (optional)   |
+--------------------------+
```

The aux section is per-layer:
- `admin`: lookup grid (cell-index sorted; binary-searchable; uncompressed)
- `water`: large-water-body feature table (zstd, may be raw if decompress fails)
- All others: empty

### 1.2 Header (256 bytes, little-endian)

Source: `scripts/shared.py:147` `HEADER_STRUCT`, `src/header.rs:46` `Header`.

| Offset | Size | Field            | Notes                                  |
|--------|------|------------------|----------------------------------------|
| 0      | 7    | magic            | See table below                        |
| 7      | 1    | null pad         | Always `0x00`                          |
| 8      | 1    | version          | u8                                     |
| 9      | 3    | pad              | Zeroed                                 |
| 12     | 4    | min_lat          | f32                                    |
| 16     | 4    | min_lon          | f32                                    |
| 20     | 4    | max_lat          | f32                                    |
| 24     | 4    | max_lon          | f32                                    |
| 28     | 8    | feature_count    | u64                                    |
| 36     | 4    | block_count      | u32                                    |
| 40     | 8    | dict_offset      | u64                                    |
| 48     | 4    | dict_length      | u32                                    |
| 52     | 8    | index_offset     | u64                                    |
| 60     | 4    | index_length     | u32                                    |
| 64     | 8    | blocks_offset    | u64                                    |
| 72     | 8    | aux_offset       | u64                                    |
| 80     | 4    | aux_length       | u32                                    |
| 84     | 8    | created_at       | u64 unix seconds (Rust reader only)    |
| 92     | 4    | (reserved)       | —                                      |
| 96     | 4    | data_version     | u32 pipeline build id                  |
| 100    | 156  | reserved         | Zeroed                                 |

### 1.3 Magic Codes and Layers

| Magic       | Layer     | File suffix             | Status                |
|-------------|-----------|-------------------------|-----------------------|
| `PTILESF\0` | Buildings | `.buildings_v8.ptiles`  | All states            |
| `PTILESR\0` | Roads     | `.roads.ptiles`         | All states + US       |
| `PTILESA\0` | Admin     | `.admin.ptiles`         | US                    |
| `PTILESW\0` | Water     | `.water.ptiles`         | All states            |
| `PTILESP\0` | Places    | `.places.ptiles`        | Selected states       |
| `PTILEST\0` | Rail      | `.rail.ptiles`          | Selected states       |
| `PTILESN\0` | Parks     | `.parks.ptiles`         | Selected states       |
| `PTILESB\0` | Business  | `.business.ptiles`      | 49 states             |
| `PTILESU\0` | Routing   | `.routing.ptiles`       | Future (see routing.md) |

### 1.4 Spatial Index (19 bytes per entry)

Source: `scripts/shared.py:203` `INDEX_ENTRY_SIZE`, `src/index.rs:30`.

```
u32  entry_count
for each entry:
  u64  h3_cell                (H3 res 7, native u64)
  u48  block_offset           (6 bytes, little-endian)
  u24  block_length           (3 bytes, little-endian)
  u16  feature_count          (clamped to 0xFFFF)
```

Entries MUST be sorted by `h3_cell` ascending (binary-searchable).
Lookup MAY be done by either binary search (admin grid) or HashMap
(per-cell readers); both are valid.

### 1.5 Offset Convention Auto-detection

Source: `src/lib.rs:40` `detect_relative_offsets`.

PTILES files ship two `block_offset` conventions:

- **Relative** (per-state exports): offsets are running totals starting at
  0; absolute file position = `header.blocks_offset + entry.block_offset`.
- **Absolute** (US-wide aggregate): offsets are absolute file positions;
  the first entry equals `header.blocks_offset`.

Detection rule (must run at open time, once):

```
relative = (first_entry.block_offset < header.blocks_offset)
        or (entries is empty)
```

All clients MUST implement this detection. Hardcoding either convention
will break either per-state or US-wide files.

### 1.6 Coordinate Encoding

Source: `src/codec.rs:52`, `scripts/shared.py:91`.

- Microdegrees: degrees × 100,000, stored as i32 (range ±21474.83°).
- First vertex: absolute `(i32 lon, i32 lat)` = 8 bytes.
- Subsequent vertices: zigzag varint deltas of (lon, lat) from previous.

Decoded coordinates are floats in `[lon, lat]` order (NOT `[lat, lon]`).
This `[lon, lat]` order is fixed by the existing Rust API
(`Vec<[f64; 2]>`) and must be preserved by all clients.

### 1.7 Varint and Zigzag

- Varint: unsigned LEB128, 7 bits per byte, high bit = continuation.
- Zigzag: `(n << 1) ^ (n >> 63)` for signed → unsigned.

### 1.8 String Encoding

- `string_u8`: 1-byte length + UTF-8 bytes (max 255).
- `string_u16`: 2-byte LE length + UTF-8 bytes.
- `indexed_or_custom`: 1-byte index into a per-layer reverse table; if the
  byte is `255`, a `string_u8` follows.

### 1.9 Zstd Compression

- Per-cell blocks are zstd-compressed.
- Dictionary (if `dict_length > 0`) is loaded once from `dict_offset` and
  used for all blocks.
- Decompression MUST fall back to non-dictionary decompress if dict
  decompress fails (admin/water depend on this; see `src/lib.rs:67`).
- Max decompressed size: 10 MiB for normal blocks, 50 MiB for aux.

### 1.10 Buildings Wall-Segment Encoding (v7+)

Source: `src/buildings.rs:358` `decode_wall_segments`.

For v7+ files, after the first absolute vertex, each subsequent vertex is
2 bytes: `(angle_byte, length_byte)`.

```
bearing_rad = (angle_byte * 360 / 256) * π / 180
length_m    = length_byte * 0.2
delta_lat   = (length_m * cos(bearing_rad)) / 111320
delta_lon   = (length_m * sin(bearing_rad)) / (111320 * cos(prev_lat_rad))
```

v6 files use ordinary zigzag-varint deltas (clients must support both).

### 1.11 Per-Layer Record Schemas

Each schema below lists fields in serialization order. `flags` is always
a single byte; bits 0..7 gate the listed optional tails.

#### Buildings (PTILESF)

```
block: { u32 record_len + record_body }*
  record_body:
    osm_id                  (v6+: varint delta from prev; v<6: u64 LE absolute)
    u8  vertex_count
    i32 first_lon, i32 first_lat
    if v >= 7: (u8 bearing, u8 length_decim)  * (vertex_count - 1)
    else:      zigzag-varint deltas           * (vertex_count - 1)
    u8 flags
    indexed_or_custom building_type    (BTYPE table; src/buildings.rs:14)
    if flags & 0x01: u16_str name
    if flags & 0x02: u8_str  category
    if flags & 0x04: u8_str  name_source
    if flags & 0x08: u64 LE  poi_osm_id
```

Centroid is computed at decode time as the unweighted mean of decoded
vertices (`src/buildings.rs:336`).

#### Roads (PTILESR)

```
block:
  { u32 record_len + record_body }*        (terminated by u32 0 sentinel)
  if version >= 2:
    u16 intersection_count
    { i32 lon_micro, i32 lat_micro, u8 int_type }*

record_body:
  varint  osm_id_delta              (from prev_osm_id, NOT zigzag — additive)
  u16     vertex_count
  i32     first_lon, i32 first_lat
  zigzag-varint deltas * (vertex_count - 1)
  u8      flags
  indexed_or_custom road_class      (ROAD_CLASS table; src/roads.rs:13)
  if flags & 0x01: u16_str name
  if flags & 0x02: u8_str  ref_tag
  if flags & 0x04: u8       oneway        (0=no, 1=forward, 2=reverse)
  if flags & 0x08: u8       speed_limit_kmh
  if flags & 0x10: u8       lanes
  if flags & 0x20: indexed_or_custom surface (SURFACE table)
  if flags & 0x40: u8       bridge_tunnel (1=bridge, 2=tunnel)

intersection_type: 1=TrafficSignals, 2=Stop, 3=GiveWay, 4=Roundabout
intersection_delay_seconds: 20, 4, 3, 2 respectively
```

#### Water (PTILESW)

```
record_body:
  varint  osm_id_delta_raw          (zigzag-decoded; can be negative)
  u8      geom_type                 (0=Polygon, 1=LineString, 2=Reference)
  if Reference:
    u32   ref_feature_id            (look up in aux feature table)
  else:
    u16   vertex_count
    i32   first_lon, i32 first_lat
    zigzag-varint deltas * (vertex_count - 1)
  u8      flags
  indexed_or_custom water_type      (WATER_TYPE table; src/water.rs:11)
  if flags & 0x01: u16_str name
  if flags & 0x02: u16 width_decim
  if flags & 0x04: u16 depth_decim  (read and discarded by Rust)

aux feature table (zstd-compressed; may be raw):
  u32 count
  { u32 feature_id, u16_str name, u8 water_type_idx,
    u32 vertex_count, i32 first_lon, i32 first_lat, zigzag-varint deltas }*
```

#### Places (PTILESP)

```
record_body:
  varint  osm_id_delta_raw          (zigzag-decoded)
  i32     lon_micro, i32 lat_micro
  u8      place_type_idx            (PLACE_TYPE table; src/places.rs:11)
  varint  population
  u16_str name
  u8      flags
  if flags & 0x01: u16_str alt_name
  if flags & 0x02: u8       admin_level
```

#### Rail (PTILEST)

```
record_body:
  varint  osm_id_delta_raw          (zigzag-decoded)
  u8      geom_type                 (0=linestring/track, 1=point/station)
  if Point:
    i32   lon_micro, i32 lat_micro
  else:
    u16   vertex_count
    i32   first_lon, i32 first_lat
    zigzag-varint deltas * (vertex_count - 1)
  u8      rail_type_idx             (RAIL_TYPE table; src/rail.rs:11)
  u8      flags
  if flags & 0x01: u16_str name
```

#### Parks (PTILESN)

```
record_body:
  varint  osm_id_delta_raw          (zigzag-decoded)
  u16     vertex_count
  i32     first_lon, i32 first_lat
  zigzag-varint deltas * (vertex_count - 1)
  u8      park_type_idx             (PARK_TYPE table; src/parks.rs:11)
  u8      flags
  if flags & 0x01: u16_str name
```

#### Admin (PTILESA)

Admin does NOT use the standard per-cell block layout. Instead:

- `dict_offset / dict_length`: zstd-compressed string tables block.
- `index_offset / index_length`: zstd-compressed polygon blob.
- `aux_offset / aux_length`: uncompressed sorted grid of H3 cells.

String tables block (after decompress):
```
{ u32 count, { u16_str entry }* }   * 5
order: country, state, county, zip, timezone
```

Grid (16 bytes per entry, sorted by h3_cell):
```
u32 entry_count
{ u64 h3_cell, u8 country_idx, u8 state_idx, u16 county_idx,
  u16 zip_idx, u8 tz_idx, u8 boundary_flags }*
```

Polygon blob (after decompress, version-dependent — current impl assumes
state-level admin_level=4):
```
u32 count
{ u8 state_idx, u16_str name, u32 vertex_count,
  { i32 lon, i32 lat }* }*           (absolute pairs, NOT delta-encoded)
```

#### Business (PTILESB v1)

Source: `scripts/build_business.py:299` `encode_record`,
`scripts/build_us_business.py:163` `encode_record`.

```
block: { u32 record_len + record_body }*

record_body:
  varint  osm_id                      (zigzag-decoded; per-state builder
                                       resets prev to 0; us-wide builder
                                       hashes the Overture id — see below)
  i32     lon_micro, i32 lat_micro
  u16_str name                        (required)
  u8      category_idx                (0 = missing; index into
                                       sidecar `_categories.json`)
  u8      flags
  if flags & 0x01: u8_str  phone
  if flags & 0x02: u8_str  website
  if flags & 0x04: u16_str address    (freeform)
  if flags & 0x08: u8_str  brand
  if flags & 0x20: u8_str  emails     (semicolon-joined)
  if flags & 0x40: u8_str  socials    (semicolon-joined)

operating_status encoding (in flags):
  0x10 alone        → permanently_closed
  0x12 (0x10|0x02)  → temporarily_closed
  neither set       → open

osm_id source: per-state US builder uses
  abs(hash(overture_id)) & 0x7FFFFFFFFFFFFFFF, encoded as a single
  zigzag varint (NOT a delta from prev). Treat osm_id as opaque u64.
```

Category names live in a sidecar JSON file next to the `.ptiles`:
`<STATE>.business_categories.json` of the shape:

```json
{ "categories": ["restaurant", "shop.convenience", ...] }
```

Indices are 1-based in the per-state builder (`cat_index[cat] = i + 1`);
index `0` means "no category". Clients MUST read the sidecar file
alongside the data file and surface `category` as a string, not an index.

---

## 2. Shared Data Model

All three client libraries expose the same logical record types. Field
names, units, and option semantics MUST match. Each section below gives
the canonical schema; per-language type signatures are in §3, §4, §5.

### 2.1 LatLng / Coordinates

- All `lat`, `lon` are WGS84 degrees as f64.
- All `coordinates` arrays are `[lon, lat][]` (longitude first).
- Distances are meters as f64 (Haversine for point queries, planar
  approximation acceptable for intra-cell graph work).

### 2.2 Record Schemas

| Field semantics for every record type:                                 |

#### Building (mirrors `src/buildings.rs:52`)

```
osm_id          u64
building_type   string                       (default: "yes")
centroid_lat    f64                          (computed)
centroid_lon    f64                          (computed)
coordinates     [lon, lat][]                 (polygon exterior, open or closed)
name            string | None
category        string | None
name_source     string | None
poi_osm_id      u64    | None
```

#### RoadSegment (mirrors `src/roads.rs:46`)

```
osm_id          u64
road_class      string                       (e.g. "motorway")
coords          [lon, lat][]
name            string | None
ref_tag         string | None
oneway          "no" | "forward" | "reverse" | None
speed_limit_kmh u8 | None
lanes           u8 | None
surface         string | None
bridge_tunnel   "bridge" | "tunnel" | None
```

#### Intersection (mirrors `src/roads.rs:68`)

```
lon_micro          i32
lat_micro          i32
intersection_type  "traffic_signals" | "stop" | "give_way" | "roundabout"
```

#### AdminInfo (mirrors `src/admin.rs:22`)

```
country         string
state           string
county          string
zip             string
timezone        string                       (IANA tz)
boundary_flags  u8
```

#### AdminPolygon (mirrors `src/admin.rs:32`)

```
name           string
admin_level    u8                            (currently 4 = state)
coordinates    [lon, lat][]
```

#### WaterFeature (mirrors `src/water.rs:47`)

```
osm_id           u64
water_type       string
geom_type        "polygon" | "linestring" | "reference"
coords           [lon, lat][]
ref_feature_id   u32 | None
name             string | None
width            u16 | None                  (decimeters)
```

#### LargeWaterBody (mirrors `src/water.rs:58`)

```
feature_id    u32
name          string
water_type    string
coords        [lon, lat][]
```

#### Place (mirrors `src/places.rs:32`)

```
osm_id       u64
lat          f64
lon          f64
place_type   string
population   u64
name         string
alt_name     string | None
admin_level  u8 | None
```

#### RailFeature (mirrors `src/rail.rs:37`)

```
osm_id      u64
rail_type   string
geom_type   0 | 1                            (0=linestring, 1=point)
coords      [lon, lat][]
name        string | None
```

#### ParkFeature (mirrors `src/parks.rs:42`)

```
osm_id     u64
park_type  string
coords     [lon, lat][]
name       string | None
```

#### Business (NEW; not yet in Rust)

```
osm_id            u64                        (opaque; per-state builder
                                              uses hashed Overture id)
lat               f64
lon               f64
name              string
category          string | None              (resolved via sidecar)
phone             string | None
website           string | None
address           string | None
brand             string | None
operating_status  "open" | "closed" | "temporarily_closed" | None
emails            string[]                   (parsed from semicolon list)
socials           string[]
```

#### Route (mirrors `src/router.rs:28`)

```
distance_meters    f64
duration_seconds   f64
from_cell          u64                       (H3 res-7 native u64)
to_cell            u64
segments           u32                       (node count along path)
path               [lon, lat][]
profile            "driving" | "walking" | "cycling"
```

#### Header (mirrors `src/header.rs:25`)

```
format         enum (Buildings, Roads, Admin, Water, Places, Rail, Parks,
                     Business, Routing)
version        u8
min_lat, min_lon, max_lat, max_lon  f32
feature_count  u64
block_count    u32
created_at     u64 (unix seconds)
data_version   u32
```

---

## 3. TypeScript/JS Client — `@ptiles/client`

Replaces the 39-line stub at `js/ptiles-client.js`. Targets browsers
(Leaflet, MapLibre) and Node. Bundle target: <500 KB minified + gzipped.

### 3.1 Dependencies

| Package              | Purpose                                  | Notes                |
|----------------------|------------------------------------------|----------------------|
| `@bokuweb/zstd-wasm` | Zstd decompress with dictionary support | wasm; ~80 KB         |
| `h3-js`              | H3 cell math (latlng↔cell, grid_disk)    | pure JS; ~120 KB     |
| (none for routing)   | Dijkstra implemented in-package          | tiny binary heap     |
| `idb`                | Optional IndexedDB cache wrapper         | only in browser ESM  |

No GIS-heavy deps (no Turf, no Leaflet runtime dep). Provide adapters as
separate sub-packages later (`@ptiles/leaflet`, `@ptiles/maplibre`).

### 3.2 Module Layout

```
packages/ptiles-client/
  src/
    index.ts                  (public re-exports)
    header.ts                 (parseHeader, Format enum)
    codec.ts                  (varint, zigzag, decode_coordinates,
                               decode_string_*, decode_indexed_or_custom)
    index.ts                  (parseIndex, lookupCell, detectRelativeOffsets)
    zstd.ts                   (wasm wrapper: decompressWithDict + fallback)
    h3.ts                     (re-export from h3-js, latlngToCell res 7)
    layers/
      buildings.ts            (BuildingsReader, decodeBuilding, wall decode)
      roads.ts                (RoadsReader, decodeRoad, decodeIntersection)
      water.ts                (WaterReader, ref resolution from aux)
      places.ts
      rail.ts
      parks.ts
      admin.ts                (binary-search grid, string tables)
      business.ts             (BusinessReader + sidecar resolver)
    router.ts                 (PtilesRouter, Dijkstra, profile filtering)
    composite.ts              (PtilesClient: queryPoint across layers)
    proximity.ts              (point-to-linestring, point-in-polygon,
                               nearestRoad, buildingsWithin)
    transport/
      file.ts                 (Node fs.promises FileHandle)
      http.ts                 (fetch with Range requests)
      idb.ts                  (IndexedDB block cache, browser only)
    types.ts                  (all interfaces from §2.2)
  package.json
  tsconfig.json
```

Public entry exports are named exports from `index.ts`. No default
exports. ESM-first; CJS shipped via build output for Node.

### 3.3 Transport / Source Abstraction

```ts
interface PTilesSource {
  size(): Promise<number>;
  read(offset: number, length: number): Promise<Uint8Array>;
  close(): Promise<void>;
}

// Built-in implementations:
function fromFile(path: string): Promise<PTilesSource>;        // Node
function fromUrl(url: string, opts?: {                          // browser/Node
  cache?: BlockCache;
  rangeRequests?: boolean;        // default true
}): PTilesSource;
function fromBlob(blob: Blob): PTilesSource;                    // browser
function fromBuffer(buf: ArrayBuffer | Uint8Array): PTilesSource;

interface BlockCache {
  get(key: string): Promise<Uint8Array | undefined>;
  set(key: string, bytes: Uint8Array): Promise<void>;
}
function idbBlockCache(dbName?: string): BlockCache;            // browser
```

`fromUrl` with `rangeRequests: true` MUST fetch only the bytes a query
needs (header, dict, index up front; one cell block per query). Without
range support it falls back to one full GET on open.

### 3.4 Per-Layer Reader API

Every layer reader follows the same shape:

```ts
abstract class LayerReader<T> {
  static async open(source: PTilesSource): Promise<this>;
  readonly header: Header;
  readonly indexSize: number;

  getInCell(cell: bigint): Promise<T[]>;
  getInBounds(minLat: number, minLon: number,
              maxLat: number, maxLon: number,
              limit?: number): Promise<T[]>;
  close(): Promise<void>;
}
```

Concrete classes (each exposing `static open` + the additional methods
listed):

```ts
class BuildingsReader extends LayerReader<Building> {
  // Returns the polygon that contains (lat,lon), else the nearest within
  // ~50 m, else null. Mirrors src/buildings.rs:117.
  query(lat: number, lon: number): Promise<Building | null>;

  // NEW: buildings whose footprint intersects a radius (meters).
  within(lat: number, lon: number, meters: number): Promise<Building[]>;
}

class RoadsReader extends LayerReader<RoadSegment> {
  getCellRoads(cell: bigint): Promise<{
    segments: RoadSegment[];
    intersections: Intersection[];
  }>;

  // NEW: nearest road to a point; searches center cell + 1-ring by
  // default. Returns null if nothing within `radiusMeters`.
  nearest(lat: number, lon: number, opts?: {
    radiusMeters?: number;       // default 100
    profile?: RoadProfile;       // optional class filter
    rings?: number;              // default 1
  }): Promise<NearestRoad | null>;

  // NEW: ranked candidates (for map-matching).
  nearestN(lat: number, lon: number, n: number, opts?: {
    radiusMeters?: number;
    profile?: RoadProfile;
  }): Promise<NearestRoad[]>;
}

interface NearestRoad {
  road: RoadSegment;
  distanceMeters: number;
  snappedLat: number;
  snappedLon: number;
  segmentIndex: number;          // which coords[i]→coords[i+1] won
  alongFraction: number;         // 0..1 along that segment
}

class WaterReader extends LayerReader<WaterFeature> {
  largeWaterBodies(): LargeWaterBody[];
  // getInBounds() must transparently resolve geom_type === "reference"
  // entries by looking up their geometry in largeWaterBodies().
}

class PlacesReader extends LayerReader<Place> {}
class RailReader   extends LayerReader<RailFeature> {}
class ParksReader  extends LayerReader<ParkFeature> {}

class AdminReader {
  static open(source: PTilesSource): Promise<AdminReader>;
  readonly header: Header;
  query(lat: number, lon: number): Promise<AdminInfo | null>;
  polygons(): Promise<AdminPolygon[]>;
  close(): Promise<void>;
}

class BusinessReader extends LayerReader<Business> {
  // The categories sidecar is fetched lazily on first query.
  static async open(source: PTilesSource, opts?: {
    categoriesSource?: PTilesSource;     // explicit override
  }): Promise<BusinessReader>;

  // NEW: ranked-by-distance proximity search.
  nearby(lat: number, lon: number, opts?: {
    radiusMeters?: number;       // default 1000
    limit?: number;              // default 10
    categoryPrefix?: string;     // e.g. "eat_and_drink.coffee"
    excludeClosed?: boolean;     // default true
  }): Promise<BusinessHit[]>;
}

interface BusinessHit {
  business: Business;
  distanceMeters: number;
}

type RoadProfile = "driving" | "walking" | "cycling";
```

### 3.5 Composite Client

```ts
class PtilesClient {
  static async open(opts: {
    buildings?: PTilesSource;
    roads?: PTilesSource;
    water?: PTilesSource;
    admin?: PTilesSource;
    places?: PTilesSource;
    rail?: PTilesSource;
    parks?: PTilesSource;
    business?: PTilesSource;
    highways?: PTilesSource;       // used as a hint for routing
  }): Promise<PtilesClient>;

  // Single-point reverse geocode + lookup across all opened layers.
  queryPoint(lat: number, lon: number, opts?: {
    includeBuildings?: boolean;    // default true
    includeAdmin?:     boolean;    // default true
    includeNearestRoad?: boolean;  // default true
    nearbyBusinessLimit?: number;  // default 5
    nearbyBusinessRadiusMeters?: number; // default 500
    waterRadiusMeters?: number;    // default 100
  }): Promise<PointReport>;

  // Bulk corridor query: features that intersect a buffered route polygon.
  corridor(path: [number, number][], bufferMeters: number, opts?: {
    layers?: ("buildings" | "business" | "roads" | "parks" | "water")[];
    limitPerLayer?: number;
  }): Promise<CorridorReport>;

  route(from: [number, number], to: [number, number],
        opts?: { profile?: RoadProfile }): Promise<Route>;

  close(): Promise<void>;
}

interface PointReport {
  building:     Building       | null;
  admin:        AdminInfo      | null;
  nearestRoad:  NearestRoad    | null;
  nearbyRoads:  NearestRoad[];          // top 5 by distance
  water:        WaterFeature[];         // within waterRadiusMeters
  parks:        ParkFeature[];          // containing or nearby
  places:       Place[];                // containing populated place
  businesses:   BusinessHit[];          // nearbyBusinessLimit
}

interface CorridorReport {
  buildings:  Building[];
  business:   Business[];
  roads:      RoadSegment[];
  parks:      ParkFeature[];
  water:      WaterFeature[];
}
```

### 3.6 Router

Mirror the existing Rust router (`src/router.rs:171`). Behavior:

```ts
class PtilesRouter {
  static async open(roads: PTilesSource): Promise<PtilesRouter>;
  attachHighways(highways: PTilesSource): Promise<void>;

  route(from: [number, number], to: [number, number],
        opts?: {
          profile?: RoadProfile;
          rings?: [number, number, number];   // default [8,16,24]
          speedFactor?: number;                // default 0.75
        }): Promise<Route>;
}
```

Implementation MUST mirror the Rust pipeline (`src/router.rs:171`–`290`):

1. Build a corridor of H3 res-7 cells along the great-circle line at
   ~1800 m step.
2. Try progressively wider rings (`[8, 16, 24]`).
3. For long routes (`from_cell.grid_distance(to_cell) > 3`) and when
   highways are attached, use highways-only for interior cells (more than
   8 cells from either endpoint) and full roads near endpoints.
4. Build a node-merging graph using union-find with a 50,000-units
   coordinate scale and a 5-unit (~11 m) merge threshold.
5. Snap origin/destination only to nodes in the largest connected
   component.
6. Dijkstra with weights in centiseconds.

### 3.7 Performance Targets

| Operation                                  | Target (Node, warm cache) |
|--------------------------------------------|---------------------------|
| `open` (header + dict + index)             | < 30 ms                   |
| `BuildingsReader.query(lat, lon)`          | < 80 ms                   |
| `RoadsReader.nearest(lat, lon)`            | < 80 ms                   |
| `AdminReader.query(lat, lon)`              | < 20 ms                   |
| `BusinessReader.nearby(...)` 1 km, top 10  | < 100 ms                  |
| `PtilesRouter.route(...)` ≤ 5 km           | < 500 ms                  |
| Bundle size (browser, min+gz)              | < 500 KB                  |

Decoded-block LRU cache, capacity 256 cells, must be on by default.

### 3.8 Error Handling

```ts
class PTilesError extends Error {
  code: "magic" | "version" | "io" | "decompress" | "parse" |
        "h3" | "router" | "category-sidecar";
  cause?: unknown;
}
```

All public methods reject with `PTilesError`. Decompression failures
inside a single block MUST NOT throw out of bulk queries; log + skip.

### 3.9 TypeScript

Ship full `.d.ts`. `strict: true`. Bigint used for H3 cells (`bigint` is
the JS-correct type for u64 — `Number` is unsafe past 2^53).

---

## 4. Python Client — `ptiles`

Pip-installable. Targets data science / batch processing. Builds on the
existing `scripts/shared.py` primitives — those move into the package
under `ptiles.codec` and the existing builder scripts are updated to
import from there.

### 4.1 Dependencies

| Package       | Purpose                       | Notes                  |
|---------------|-------------------------------|------------------------|
| `zstandard`   | Zstd with dictionary support  | Required               |
| `h3` (v4)     | H3 cell math                  | Required               |
| `numpy`       | Coordinate arrays             | Required               |
| `pandas`      | DataFrame return types        | Optional extra `[pd]`  |
| `geopandas`   | GeoDataFrame return types     | Optional extra `[gpd]` |
| `shapely`     | Point-in-polygon, geometry    | Optional extra `[geo]` |

No mandatory C extensions beyond what `zstandard` ships. The library
must work without geopandas; `as_geodataframe()` raises a clear ImportError
if the extra is missing.

### 4.2 Package Layout

```
ptiles/
  __init__.py            (re-exports public API)
  header.py              (Header, Format, read_header, MAGIC_TO_FORMAT)
  codec.py               (varint, zigzag, coordinate/string decoders;
                          moved from scripts/shared.py)
  index.py               (Index, IndexEntry, binary_search,
                          detect_relative_offsets)
  zstd_io.py             (open_with_dict, decompress_block_with_fallback)
  layers/
    __init__.py
    buildings.py         (BuildingsReader, Building dataclass,
                          v6/v7 record parsers)
    roads.py             (RoadsReader, RoadSegment, Intersection,
                          IntersectionType enum)
    water.py             (WaterReader, WaterFeature, LargeWaterBody,
                          GeomType enum, ref resolution)
    places.py
    rail.py
    parks.py
    admin.py             (AdminReader, AdminInfo, AdminPolygon, grid)
    business.py          (BusinessReader, Business, categories sidecar)
  proximity.py           (haversine, point_to_segment_meters,
                          point_in_polygon, nearest_road,
                          buildings_within)
  router.py              (PtilesRouter, Route, Dijkstra)
  composite.py           (PtilesClient.query_point, corridor)
  io.py                  (PTilesSource ABC; FilePTilesSource,
                          MemoryPTilesSource)
  exceptions.py          (PTilesError + subclasses)
  cli.py                 (entry-point: `python -m ptiles inspect file`)
  py.typed
tests/
  ...
pyproject.toml
```

### 4.3 Source Abstraction

```python
class PTilesSource(Protocol):
    def size(self) -> int: ...
    def read(self, offset: int, length: int) -> bytes: ...
    def close(self) -> None: ...

class FilePTilesSource(PTilesSource):
    def __init__(self, path: str | os.PathLike): ...

class MemoryPTilesSource(PTilesSource):
    def __init__(self, data: bytes | memoryview): ...
```

### 4.4 Dataclasses (mirror §2.2 one-to-one)

```python
@dataclass(frozen=True, slots=True)
class Building:
    osm_id: int
    building_type: str
    centroid_lat: float
    centroid_lon: float
    coordinates: list[tuple[float, float]]   # (lon, lat)
    name: str | None = None
    category: str | None = None
    name_source: str | None = None
    poi_osm_id: int | None = None

@dataclass(frozen=True, slots=True)
class RoadSegment:
    osm_id: int
    road_class: str
    coords: list[tuple[float, float]]
    name: str | None = None
    ref_tag: str | None = None
    oneway: Literal["no", "forward", "reverse"] | None = None
    speed_limit_kmh: int | None = None
    lanes: int | None = None
    surface: str | None = None
    bridge_tunnel: Literal["bridge", "tunnel"] | None = None

class IntersectionType(IntEnum):
    TRAFFIC_SIGNALS = 1
    STOP = 2
    GIVE_WAY = 3
    ROUNDABOUT = 4

@dataclass(frozen=True, slots=True)
class Intersection:
    lon_micro: int
    lat_micro: int
    intersection_type: IntersectionType

class GeomType(IntEnum):
    POLYGON = 0
    LINESTRING = 1
    REFERENCE = 2

@dataclass(frozen=True, slots=True)
class WaterFeature:
    osm_id: int
    water_type: str
    geom_type: GeomType
    coords: list[tuple[float, float]]
    ref_feature_id: int | None = None
    name: str | None = None
    width: int | None = None

@dataclass(frozen=True, slots=True)
class AdminInfo:
    country: str
    state: str
    county: str
    zip: str
    timezone: str
    boundary_flags: int

@dataclass(frozen=True, slots=True)
class Place:
    osm_id: int
    lat: float
    lon: float
    place_type: str
    population: int
    name: str
    alt_name: str | None = None
    admin_level: int | None = None

@dataclass(frozen=True, slots=True)
class RailFeature:
    osm_id: int
    rail_type: str
    geom_type: int          # 0 linestring, 1 point
    coords: list[tuple[float, float]]
    name: str | None = None

@dataclass(frozen=True, slots=True)
class ParkFeature:
    osm_id: int
    park_type: str
    coords: list[tuple[float, float]]
    name: str | None = None

@dataclass(frozen=True, slots=True)
class Business:
    osm_id: int
    lat: float
    lon: float
    name: str
    category: str | None = None
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    brand: str | None = None
    operating_status: Literal["open", "closed", "temporarily_closed"] | None = None
    emails: tuple[str, ...] = ()
    socials: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class Route:
    distance_meters: float
    duration_seconds: float
    from_cell: int
    to_cell: int
    segments: int
    path: list[tuple[float, float]]
    profile: str
```

### 4.5 Per-Layer Readers

```python
class BuildingsReader:
    @classmethod
    def open(cls, source: PTilesSource | str | Path) -> "BuildingsReader": ...
    header: Header

    def query(self, lat: float, lon: float) -> Building | None: ...
    def within(self, lat: float, lon: float, meters: float) -> list[Building]: ...
    def get_in_cell(self, cell: int) -> list[Building]: ...
    def get_in_bounds(self, min_lat: float, min_lon: float,
                      max_lat: float, max_lon: float,
                      limit: int = 1000) -> list[Building]: ...
    def as_dataframe(self, *,
                     bounds: tuple[float, float, float, float] | None = None
                    ) -> "pandas.DataFrame": ...
    def as_geodataframe(self, *,
                        bounds: tuple[float, float, float, float] | None = None
                       ) -> "geopandas.GeoDataFrame": ...
    def close(self) -> None: ...

class RoadsReader:
    @classmethod
    def open(cls, source) -> "RoadsReader": ...
    header: Header

    def get_in_cell(self, cell: int) -> list[RoadSegment]: ...
    def get_cell_roads(self, cell: int) -> tuple[list[RoadSegment],
                                                 list[Intersection]]: ...
    def get_in_bounds(self, min_lat: float, min_lon: float,
                      max_lat: float, max_lon: float,
                      limit: int = 1000) -> list[RoadSegment]: ...

    def nearest(self, lat: float, lon: float, *,
                radius_meters: float = 100,
                profile: str | None = None,
                rings: int = 1) -> NearestRoad | None: ...
    def nearest_n(self, lat: float, lon: float, n: int = 5, *,
                  radius_meters: float = 100,
                  profile: str | None = None) -> list[NearestRoad]: ...

@dataclass(frozen=True, slots=True)
class NearestRoad:
    road: RoadSegment
    distance_meters: float
    snapped_lat: float
    snapped_lon: float
    segment_index: int
    along_fraction: float

class WaterReader:
    @classmethod
    def open(cls, source) -> "WaterReader": ...
    def large_water_bodies(self) -> list[LargeWaterBody]: ...
    def get_in_cell(self, cell: int) -> list[WaterFeature]: ...
    def get_in_bounds(self, min_lat, min_lon, max_lat, max_lon,
                      limit: int = 1000) -> list[WaterFeature]: ...

class PlacesReader:    # mirror shape; query/get_in_bounds
    ...
class RailReader:      # mirror shape
    ...
class ParksReader:     # mirror shape
    ...

class AdminReader:
    @classmethod
    def open(cls, source) -> "AdminReader": ...
    def query(self, lat: float, lon: float) -> AdminInfo | None: ...
    def polygons(self) -> list[AdminPolygon]: ...

class BusinessReader:
    @classmethod
    def open(cls, source, *,
             categories: list[str] | str | Path | None = None
            ) -> "BusinessReader": ...
    """If `categories` is None, the reader auto-locates
       `<basename>_categories.json` alongside the data file."""

    def nearby(self, lat: float, lon: float, *,
               radius_meters: float = 1000,
               limit: int = 10,
               category_prefix: str | None = None,
               exclude_closed: bool = True) -> list[BusinessHit]: ...
    def get_in_bounds(self, *bounds, limit: int = 1000) -> list[Business]: ...

@dataclass(frozen=True, slots=True)
class BusinessHit:
    business: Business
    distance_meters: float
```

### 4.6 Composite Client

```python
class PtilesClient:
    @classmethod
    def open(cls, *,
             buildings: str | Path | None = None,
             roads:     str | Path | None = None,
             water:     str | Path | None = None,
             admin:     str | Path | None = None,
             places:    str | Path | None = None,
             rail:      str | Path | None = None,
             parks:     str | Path | None = None,
             business:  str | Path | None = None,
             highways:  str | Path | None = None) -> "PtilesClient": ...

    @classmethod
    def open_state(cls, state: str, data_dir: str | Path) -> "PtilesClient":
        """Open all available `<STATE>.<layer>.ptiles` in data_dir."""

    def query_point(self, lat: float, lon: float, *,
                    include_buildings: bool = True,
                    include_admin: bool = True,
                    include_nearest_road: bool = True,
                    nearby_business_limit: int = 5,
                    nearby_business_radius_meters: float = 500,
                    water_radius_meters: float = 100
                   ) -> PointReport: ...

    def corridor(self, path: list[tuple[float, float]],
                 buffer_meters: float, *,
                 layers: list[str] | None = None,
                 limit_per_layer: int = 5000) -> CorridorReport: ...

    def route(self, src: tuple[float, float], dst: tuple[float, float], *,
              profile: str = "driving") -> Route: ...

@dataclass
class PointReport:
    building: Building | None
    admin: AdminInfo | None
    nearest_road: NearestRoad | None
    nearby_roads: list[NearestRoad]
    water: list[WaterFeature]
    parks: list[ParkFeature]
    places: list[Place]
    businesses: list[BusinessHit]

@dataclass
class CorridorReport:
    buildings: list[Building]
    business:  list[Business]
    roads:     list[RoadSegment]
    parks:     list[ParkFeature]
    water:     list[WaterFeature]
```

### 4.7 Performance Targets

| Operation                                | Target (CPython 3.12, warm) |
|------------------------------------------|-----------------------------|
| Reader `open`                            | < 50 ms                     |
| `BuildingsReader.query`                  | < 80 ms                     |
| `RoadsReader.nearest`                    | < 100 ms                    |
| `AdminReader.query`                      | < 10 ms                     |
| `BusinessReader.nearby` 1 km, top 10     | < 80 ms                     |
| `PtilesRouter.route` ≤ 5 km              | < 1500 ms                   |
| Cold open + first query                  | < 250 ms                    |

`get_in_bounds` MAY use numpy bulk decoding for hot paths; the public
return type stays list-of-dataclass for parity with Rust/JS, but a
`as_arrays()` helper returning a dict-of-numpy is acceptable.

### 4.8 Error Handling

```python
class PTilesError(Exception): ...
class MagicError(PTilesError): ...
class VersionError(PTilesError): ...
class IndexError(PTilesError): ...
class DecompressError(PTilesError): ...
class ParseError(PTilesError): ...
class CategorySidecarError(PTilesError): ...
class RouterError(PTilesError): ...
```

Per-record parse errors inside `get_in_*` MUST be logged via the
standard `logging` module (`ptiles` namespace) and the bad record
skipped; never abort a bulk query.

### 4.9 CLI

```
python -m ptiles inspect FILE                 # print header summary
python -m ptiles query buildings FILE LAT LON
python -m ptiles nearest-road FILE LAT LON
python -m ptiles nearby business FILE LAT LON --radius 500 --limit 5
python -m ptiles route ROADS.ptiles A_LAT A_LON B_LAT B_LON
```

### 4.10 Migration Notes

The existing scripts in `~/kino/projects/ptiles/scripts/` keep working.
They re-import the moved primitives from `ptiles.codec` instead of
`shared.py`, so functionality is unchanged. Missing decoders today
(`buildings`, `rail`, `parks`, `places`, `business`) are gained
automatically once the package is installed.

---

## 5. Rust Client — `ptiles` (existing crate upgrade)

Crate at `~/kino/projects/timeline/ptiles/`. Today exposes 7 layer
readers + router. The upgrade adds the missing pieces called out in the
brief.

### 5.1 New Modules

```
src/
  business.rs           (NEW — BusinessReader, Business)
  proximity.rs          (NEW — nearest_point_on_segment, NearestRoad,
                         point_to_linestring_meters)
  composite.rs          (NEW — PtilesClient.query_point, corridor)
  categories.rs         (NEW — sidecar JSON loader for business categories)
  lib.rs                (re-exports new types)
```

`router.rs`, `roads.rs`, `buildings.rs`, etc. stay where they are.

### 5.2 Business Reader

Add `pub use business::{Business, BusinessError, BusinessReader};` to
`lib.rs` and extend `Format` enum and the `header::detect_format`
function to recognise `PTILESB\0`.

```rust
#[derive(Debug, Clone, Serialize)]
pub struct Business {
    pub osm_id: u64,
    pub lat: f64,
    pub lon: f64,
    pub name: String,
    pub category: Option<String>,
    pub phone: Option<String>,
    pub website: Option<String>,
    pub address: Option<String>,
    pub brand: Option<String>,
    pub operating_status: Option<OperatingStatus>,
    pub emails: Vec<String>,
    pub socials: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum OperatingStatus {
    Open,
    Closed,
    TemporarilyClosed,
}

#[derive(Debug, Clone, Serialize)]
pub struct BusinessHit {
    pub business: Business,
    pub distance_meters: f64,
}

#[derive(Debug, thiserror::Error)]
pub enum BusinessError {
    #[error("IO error: {0}")]      Io(#[from] std::io::Error),
    #[error("Decompression error")] Decompress,
    #[error("Parse error: {0}")]   Parse(String),
    #[error("Categories sidecar error: {0}")] Categories(String),
}

pub struct BusinessReader {
    file: File,
    header: Header,
    index: Index,
    dict_data: Vec<u8>,
    relative_offsets: bool,
    categories: Vec<String>,             // index → name, loaded from sidecar
}

impl BusinessReader {
    /// Looks for `<basename>_categories.json` next to the data file.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self, BusinessError>;

    pub fn open_with_categories<P: AsRef<Path>>(
        path: P, categories: Vec<String>,
    ) -> Result<Self, BusinessError>;

    pub fn header(&self) -> &Header;

    pub fn get_business_in_cell(&mut self, cell: CellIndex)
        -> Result<Vec<Business>, BusinessError>;

    pub fn get_business_in_bounds(
        &mut self,
        min_lat: f64, min_lon: f64, max_lat: f64, max_lon: f64,
        limit: usize,
    ) -> Result<Vec<Business>, BusinessError>;

    pub fn nearby(
        &mut self,
        lat: f64, lon: f64,
        radius_meters: f64,
        limit: usize,
        category_prefix: Option<&str>,
        exclude_closed: bool,
    ) -> Result<Vec<BusinessHit>, BusinessError>;
}
```

Record parsing follows §1.11 Business (PTILESB v1).

### 5.3 Road Proximity

```rust
// src/proximity.rs
#[derive(Debug, Clone, Serialize)]
pub struct NearestRoad {
    pub road: RoadSegment,
    pub distance_meters: f64,
    pub snapped_lat: f64,
    pub snapped_lon: f64,
    pub segment_index: usize,
    pub along_fraction: f64,
}

impl RoadsReader {
    /// Nearest road across the H3 res-7 cell containing (lat,lon) plus
    /// `rings` rings of neighbours (0 = center only, 1 = +6 cells).
    /// Filters by `profile` if Some.
    pub fn nearest(
        &mut self,
        lat: f64, lon: f64,
        radius_meters: f64,
        profile: Option<&str>,
        rings: u32,
    ) -> Result<Option<NearestRoad>, RoadsError>;

    pub fn nearest_n(
        &mut self,
        lat: f64, lon: f64,
        n: usize,
        radius_meters: f64,
        profile: Option<&str>,
    ) -> Result<Vec<NearestRoad>, RoadsError>;
}
```

Distance calculation: planar approximation with latitude-scale factor
(same as `src/router.rs:404`); convert decoded lon/lat to meters and
compute point-to-segment distance for each segment.

### 5.4 Composite Query

```rust
// src/composite.rs
pub struct PtilesClient {
    pub buildings: Option<BuildingsReader>,
    pub roads:     Option<RoadsReader>,
    pub water:     Option<WaterReader>,
    pub admin:     Option<AdminReader>,
    pub places:    Option<PlacesReader>,
    pub rail:      Option<RailReader>,
    pub parks:     Option<ParkReader>,
    pub business:  Option<BusinessReader>,
    pub router:    Option<PtilesRouter>,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct PointReport {
    pub building:     Option<Building>,
    pub admin:        Option<AdminInfo>,
    pub nearest_road: Option<NearestRoad>,
    pub nearby_roads: Vec<NearestRoad>,
    pub water:        Vec<WaterFeature>,
    pub parks:        Vec<ParkFeature>,
    pub places:       Vec<Place>,
    pub businesses:   Vec<BusinessHit>,
}

#[derive(Debug, Clone, Default)]
pub struct PointQueryOpts {
    pub include_buildings: bool,
    pub include_admin: bool,
    pub include_nearest_road: bool,
    pub nearby_business_limit: usize,
    pub nearby_business_radius_meters: f64,
    pub water_radius_meters: f64,
}

impl PtilesClient {
    pub fn open_state<P: AsRef<Path>>(state: &str, data_dir: P)
        -> Result<Self, PtilesError>;

    pub fn query_point(
        &mut self,
        lat: f64, lon: f64,
        opts: &PointQueryOpts,
    ) -> Result<PointReport, PtilesError>;

    pub fn corridor(
        &mut self,
        path: &[[f64; 2]],
        buffer_meters: f64,
        layers: &[&str],
        limit_per_layer: usize,
    ) -> Result<CorridorReport, PtilesError>;
}
```

`open_state` walks `<data_dir>/<STATE>.<suffix>.ptiles` for each known
suffix (`buildings_v8`, `roads`, `water`, `business`, `places`, `rail`,
`parks`) plus an optional `<data_dir>/US.highways.ptiles`.

### 5.5 SIMD Coordinate Decode

Add a feature flag `simd` (off by default):

```rust
#[cfg(feature = "simd")]
mod codec_simd {
    pub fn decode_zigzag_varint_pairs(
        data: &[u8],
        out: &mut Vec<[i32; 2]>,
        first_lon: i32, first_lat: i32,
        vertex_count: usize,
    ) -> usize;
}
```

Use `std::simd` or `wide` crate; provide a scalar fallback that matches
`codec::decode_coordinates` byte-for-byte. Benchmark target: ≥2× scalar
on a typical `RoadSegment` decode of 32 vertices.

### 5.6 Async File I/O

Add a feature flag `tokio` and parallel readers:

```rust
#[cfg(feature = "tokio")]
pub struct AsyncBuildingsReader { /* tokio::fs::File backing */ }

#[cfg(feature = "tokio")]
impl AsyncBuildingsReader {
    pub async fn open(path: impl AsRef<Path>) -> Result<Self, BuildingsError>;
    pub async fn query(&mut self, lat: f64, lon: f64)
        -> Result<Option<Building>, BuildingsError>;
    pub async fn get_in_cell(&mut self, cell: CellIndex)
        -> Result<Vec<Building>, BuildingsError>;
}
```

Same set for `RoadsReader`, `BusinessReader`, `AdminReader`. The sync
APIs remain the default; async is purely additive.

### 5.7 Performance Targets

| Operation                                 | Target (release, warm)       |
|-------------------------------------------|------------------------------|
| `Reader::open`                            | < 5 ms                       |
| `BuildingsReader::query`                  | < 10 ms                      |
| `RoadsReader::nearest`                    | < 10 ms                      |
| `AdminReader::query`                      | < 1 ms                       |
| `BusinessReader::nearby` 1 km, top 10     | < 15 ms                      |
| `PtilesRouter::route` ≤ 5 km              | < 80 ms                      |
| `PtilesClient::query_point` all layers    | < 50 ms                      |

### 5.8 Error Handling

Extend `PtilesError` in `src/lib.rs:118`:

```rust
#[derive(Debug, thiserror::Error)]
pub enum PtilesError {
    // existing variants...
    #[error("Business error: {0}")]
    Business(#[from] BusinessError),
    #[error("Router error: {0}")]
    Router(#[from] RouterError),
    #[error("Composite error: {0}")]
    Composite(String),
}
```

Per-record decode errors continue the existing convention: bulk
`get_in_*` calls skip bad records; single-point queries propagate the
first hard parse error.

---

## 6. Cross-Cutting Requirements

### 6.1 Test Fixtures

All three clients MUST include the following golden-vector tests, run
against the live data at `~/kino/projects/ptiles/data/states/`:

| File                   | Query                          | Expected                              |
|------------------------|--------------------------------|---------------------------------------|
| `TN.roads.ptiles`      | nearest(36.1627, -86.7816)     | road within 50 m, named in coverage   |
| `TN.buildings_v8.ptiles` | query(36.1627, -86.7816)     | Some(Building) with name present      |
| `US.admin.ptiles`*     | query(36.1627, -86.7816)       | state="Tennessee", county!=""         |
| `US.admin.ptiles`*     | query(35.0, -60.0)             | None (ocean)                          |
| `TN.water.ptiles`      | bounds query around Nashville  | non-empty, ref types resolved         |
| `TN.business.ptiles`   | nearby(36.1627,-86.7816,1000,10) | ≥1 result with non-empty name       |
| Router                 | Nashville → Chattanooga driving | 2.0–2.5 h, 250–320 km                |

`*` Admin file is the US-wide aggregate when present; otherwise skip.

### 6.2 Versioning

- Clients MUST accept multiple `header.version` values per layer:
  - Buildings: v6 (varint osm_id, zigzag coords) and v7+ (wall encoding).
  - Roads: v1 (no intersection table) and v2+ (intersection table).
  - All others: current version only; reject unknowns with `VersionError`.
- Format magic mismatch → `MagicError` with the actual bytes echoed back.
- Clients SHOULD warn when `header.data_version` is older than the
  client's known minimum useful value (no hard reject).

### 6.3 Coordinate Order Discipline

Every public API exposes:
- Scalar inputs as `(lat, lon)` order in argument lists.
- Coordinate arrays as `[lon, lat]` pairs.

This matches the existing Rust API (`pub fn query(&mut self, lat: f64,
lon: f64)` returning `coordinates: Vec<[f64; 2]>`) and existing decoder
scripts. Do not reverse either convention; do not mix.

### 6.4 H3 Resolution

- All spatial indexes are H3 resolution 7. Clients hardcode `7`; do not
  read it from the header.
- H3 cell type is `u64` (or `bigint` in JS).

### 6.5 Concurrency

- Rust: readers are `!Sync` (mutate file cursor). Multi-thread use goes
  through `Arc<Mutex<Reader>>` or one reader per thread.
- Python: thread-safe assumed by users via the GIL, but `PtilesSource`
  reads MUST be atomic — implementations using `pread`/`os.pread` are
  preferred over `seek` + `read`.
- JS: every Promise-returning method must be safe to issue concurrently;
  internal state (cache, source) must serialise reads as needed.

### 6.6 What is explicitly NOT in scope

- No write/builder API in any client. Builders stay in
  `scripts/build_*.py`.
- No tile rendering. Provide raw geometry; let downstream Leaflet /
  MapLibre adapters consume it.
- No network sync of category sidecars. The JSON file must already be
  alongside the `.ptiles` file (matches the current production layout).

---

## 7. Implementation Order (suggested)

1. **Rust BusinessReader + categories sidecar** — unblocks composite
   queries on the largest by-file-count layer.
2. **Rust RoadsReader::nearest + NearestRoad** — needed by every
   composite report and by the JS/Python clients (shared algorithm).
3. **Rust composite::PtilesClient::query_point** — proves the
   cross-layer contract before duplicating it in two more languages.
4. **Python package skeleton** — move `shared.py` into `ptiles.codec`;
   port the missing decoders (buildings, rail, parks, places, business)
   from Rust.
5. **TypeScript package skeleton** — header + codec + index + all layer
   readers, sharing the §1 spec.
6. **PtilesRouter port (TS, Python)** — last; depends on the roads
   reader and `NearestRoad`.
7. **SIMD + async Rust extensions** — feature-flagged, additive.
