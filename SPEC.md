# PTiles Multi-Layer Specification

Comprehensive offline GPS context format. Given any coordinate, return everything useful about that location — building, road, address, admin region, water, parks, transit, and nearby POIs.

**Total US coverage: ~2.1–3.2 GB** (vs hundreds of GB source data).

---

## Table of Contents

- [Architecture](#architecture)
- [Shared Format](#shared-format)
  - [Header](#header-256-bytes)
  - [Spatial Index](#spatial-index)
  - [Coordinate Encoding](#coordinate-encoding)
  - [Varint / Zigzag Encoding](#varint--zigzag-encoding)
- [Layer: Buildings (F)](#layer-buildings-f) — existing v6 format
- [Layer: Roads (R)](#layer-roads-r)
- [Layer: Admin Boundaries (A)](#layer-admin-boundaries-a)
- [Layer: Water (W)](#layer-water-w)
- [Layer: Places (P)](#layer-places-p)
- [Layer: Parks (N)](#layer-parks-n)
- [Layer: Rail & Transit (T)](#layer-rail--transit-t)
- [Layer: POIs (I)](#layer-pois-i)
- [Layer: Address Ranges (D)](#layer-address-ranges-d)
- [Layer: Routing (U)](#layer-routing-u)
- [Combined Query](#combined-query)
- [Data Sources](#data-sources)
- [Reference Decoders](#reference-decoders)

---

## Architecture

Each layer is a separate file with its own schema optimized for its data characteristics. All files share the same header structure, spatial index format, and compression primitives.

| Name | Magic | Layer | Geometry | Est. Size |
|------|-------|-------|----------|-----------|
| `US.ptiles` | `PTILESF\\x00` | Buildings | Small polygons | ~1.14 GB |
| `US.roads.ptiles` | `PTILESR\\x00` | Roads | LineStrings (split at cell boundaries) | ~0.75–1.0 GB |
| `US.admin.ptiles` | `PTILESA\\x00` | Admin + ZIP + TZ | H3 lookup grid + large polygons | ~50–100 MB |
| `US.water.ptiles` | `PTILESW\\x00` | Water bodies | Mixed polygon + linestring | ~200–400 MB |
| `US.places.ptiles` | `PTILESP\\x00` | Place names | Points | ~20–50 MB |
| `US.parks.ptiles` | `PTILESN\\x00` | Parks & protected areas | H3 lookup grid + polygons | ~30–80 MB |
| `US.rail.ptiles` | `PTILEST\\x00` | Rail & transit | LineStrings + points | ~30–60 MB |
| `US.poi.ptiles` | `PTILESI\\x00` | POIs | Points | ~50–150 MB |
| `US.addr.ptiles` | `PTILESD\\x00` | Address ranges | LineStrings + metadata | ~200–500 MB |
| `TN.routing.ptiles` | `PTILESU\\x00` | Routing | Portal graphs (no geometry) | ~50 MB/TN, ~2 GB/US |

**Why separate files?** Each data type has radically different feature density (77M buildings vs 3K admin regions), geometry characteristics (5-vertex polygons vs 50K-vertex state borders), optimal compression, and query patterns. Separate files let each be independently optimized, cached, and updated.

Three encoding paradigms:

| Paradigm | Used by | Description |
|----------|---------|-------------|
| **Per-cell features** | Buildings, Roads, Water, Rail, POIs, Addresses | Features stored in H3 cell blocks, one feature per record |
| **H3 lookup grid** | Admin, Parks | Pre-computed answer per H3 cell, O(log n) binary search |

---

## Shared Format

All PTiles files use these common structures.

### Header (256 bytes)

| Offset | Size | Type | Field | Description |
|--------|------|------|-------|-------------|
| 0 | 7 | bytes | magic_prefix | `PTILES` + layer byte (see table) |
| 7 | 1 | uint8 | magic_null | `\x00` terminator |
| 8 | 1 | uint8 | version | Schema version (current: 6 for buildings, 1 for new layers) |
| 9 | 3 | — | reserved | Alignment padding |
| 12 | 4 | float32 | min_lat | Bounding box south |
| 16 | 4 | float32 | min_lon | Bounding box west |
| 20 | 4 | float32 | max_lat | Bounding box north |
| 24 | 4 | float32 | max_lon | Bounding box east |
| 28 | 8 | uint64 | feature_count | Total feature/record count |
| 36 | 4 | uint32 | block_count | Number of H3 cell blocks |
| 40 | 8 | uint64 | dict_offset | Byte offset to zstd dictionary |
| 48 | 4 | uint32 | dict_length | Dictionary size in bytes |
| 52 | 8 | uint64 | index_offset | Byte offset to spatial index |
| 60 | 4 | uint32 | index_length | Index size in bytes |
| 64 | 8 | uint64 | blocks_offset | Byte offset to first data block |
| 72 | 8 | uint64 | aux_offset | Byte offset to auxiliary section (0 if unused) |
| 80 | 4 | uint32 | aux_length | Auxiliary section size (0 if unused) |
| 84 | 172 | — | reserved | Future use (zeroed) |

**Byte order:** Little-endian throughout.

**Magic layer bytes:**

| Byte | ASCII | Layer |
|------|-------|-------|
| `0x46` | `F` | Buildings (footprints) |
| `0x52` | `R` | Roads |
| `0x41` | `A` | Admin boundaries |
| `0x57` | `W` | Water |
| `0x50` | `P` | Places |
| `0x4E` | `N` | Parks (nature/protected) |
| `0x54` | `T` | Rail/transit |
| `0x49` | `I` | POIs (interests) |
| `0x44` | `D` | Address ranges (delivery) |
| `0x55` | `U` | Routing (Urban navigation) |

**Auxiliary section** (`aux_offset`/`aux_length`): Used by layers that need an additional data structure beyond the standard header → dictionary → index → blocks layout. Admin and Parks layers use this for their lookup grids. Other layers set these fields to 0.

### Spatial Index

H3 resolution 7 cells (~5.16 km² average). Sorted by H3 cell ID for binary search.

```
┌──────────────────────────────────────────────────────────────┐
│ entry_count (4 bytes, uint32)                                │
├──────────────────────────────────────────────────────────────┤
│ Entry 0                                                      │
│   h3_cell    (8 bytes, uint64) — H3 index as integer         │
│   block_offset (6 bytes)      — Absolute byte offset         │
│   block_length (3 bytes)      — Compressed block size        │
│   feature_count (2 bytes, uint16) — Features in this cell    │
├──────────────────────────────────────────────────────────────┤
│ Entry 1…N  (19 bytes each)                                   │
└──────────────────────────────────────────────────────────────┘
```

Entry size: **19 bytes** (8 + 6 + 3 + 2).

6-byte offset supports files up to 281 TB. 3-byte length supports blocks up to 16 MB.

### Data Blocks

Each block is **zstd compressed** (level 22) with a shared trained dictionary. Contains all features whose centroid (or, for linestrings, whose segment) falls within the H3 cell.

Decompressed format:

```
┌──────────────────────────────────────────────────────────────┐
│ Record 0                                                     │
│   record_length (4 bytes, uint32) — Size of record data      │
│   record_data  (variable)        — Layer-specific record     │
├──────────────────────────────────────────────────────────────┤
│ Record 1…N                                                   │
└──────────────────────────────────────────────────────────────┘
```

Features within a block are sorted by OSM ID (or source ID) for delta encoding.

### Coordinate Encoding

**Precision:** Microdegrees (degrees × 100,000) stored as int32.

| Property | Value |
|----------|-------|
| 1 unit | ~1.1 m at equator, ~0.7 m at 50° lat |
| int32 range | ±21,474° (covers Earth) |
| Typical building wall delta | ±5,000 units (±55 m) |

First coordinate in each feature stored absolute (int32 lon, int32 lat = 8 bytes). Subsequent vertices stored as zigzag varint delta pairs.

### Varint / Zigzag Encoding

**Zigzag** maps signed → unsigned (small magnitudes → small values):

```
zigzag(n) = (n << 1) ^ (n >> 31)

 0 → 0,  -1 → 1,  1 → 2,  -2 → 3,  2 → 4, …
```

**Varint** (protobuf-style, 7 bits/byte, MSB = continuation):

```
while value >= 0x80:
    emit(0x80 | (value & 0x7F))
    value >>= 7
emit(value)
```

| Value range | Bytes |
|-------------|-------|
| 0–127 | 1 |
| 128–16,383 | 2 |
| 16,384–2,097,151 | 3 |

### Zstd Dictionary

Each layer trains its own dictionary on a representative sample of ~10,000 features. Typical dictionary size: 512 KB. Stored immediately after the header.

### HTTP Range Request Pattern

For hosted files, cache header + dictionary + index on client (~1 MB per layer). Each query requires 1 range request per layer for the data block (~2–50 KB compressed).

```
GET /US.roads.ptiles
Range: bytes=0-786432            # Header + dict + index (once, cached)

GET /US.roads.ptiles
Range: bytes=12345678-12348000   # Single block per query
```

---

## Layer: Buildings (F)

**Magic:** `PTILESF\x00` — **Status:** Implemented (v6)

See [README.md](./README.md) for the full v6 building schema. Summary:

| Field | Encoding | Description |
|-------|----------|-------------|
| osm_id | varint (delta) | Delta from previous OSM ID in block |
| vertex_count | uint8 | Polygon vertex count (max 255) |
| first_lon | int32 | First longitude × 100,000 |
| first_lat | int32 | First latitude × 100,000 |
| deltas | zigzag varint pairs | Delta lon/lat per subsequent vertex |
| flags | uint8 | Bitmask for optional fields |
| btype_idx | uint8 | Building type (20 indexed + 255=custom) |
| [name] | uint16 len + UTF-8 | If flags & 0x01 |
| [category] | uint8 len + UTF-8 | If flags & 0x02 |
| [name_source] | uint8 len + UTF-8 | If flags & 0x04 |
| [poi_osm_id] | uint64 | If flags & 0x08 |
| [height] | uint8 | If flags & 0x10 (0.5 m steps, 0–127.5 m) |

**Query:** GPS → H3 cell → binary search → decompress → point-in-polygon test.

**Size:** ~1.14 GB (77M buildings, ~15 bytes/building).

---

## Layer: Roads (R)

**Magic:** `PTILESR\x00`

Road segments from OpenStreetMap. Each OSM way is **split at H3 cell boundaries** so every segment resides in exactly one cell block. The original `osm_way_id` is preserved for rejoining.

### Segment Splitting

```
Original OSM way (crosses 3 H3 cells):
  ┌───────┐  ┌───────┐  ┌───────┐
  │ Cell A │──│ Cell B │──│ Cell C │
  └───────┘  └───────┘  └───────┘

Stored as 3 separate records, each in its cell's block,
all sharing the same osm_way_id.
```

Splitting increases record count by ~30–40% but preserves the uniform query model: look up one cell, get all road segments in it.

### Record Format

| Field | Encoding | Description |
|-------|----------|-------------|
| osm_way_id | varint (delta) | Delta from previous ID in block |
| vertex_count | uint16 | Vertex count (uint16 — roads can exceed 255 vertices) |
| first_lon | int32 | First longitude × 100,000 |
| first_lat | int32 | First latitude × 100,000 |
| deltas | zigzag varint pairs | Delta lon/lat per subsequent vertex |
| flags | uint8 | Bitmask for optional fields |
| road_class | uint8 | Indexed road type (see table) |
| [name] | uint16 len + UTF-8 | If flags & 0x01 |
| [ref] | uint8 len + UTF-8 | If flags & 0x02 — route ref (e.g., "I-95", "US-1") |
| [oneway] | uint8 | If flags & 0x04 — 0=no, 1=forward, 2=reverse |
| [speed_limit] | uint8 | If flags & 0x08 — km/h (0–255) |
| [lanes] | uint8 | If flags & 0x10 — total lane count |
| [surface] | uint8 | If flags & 0x20 — indexed surface type |
| [bridge_tunnel] | uint8 | If flags & 0x40 — 0=neither, 1=bridge, 2=tunnel |

### Road Class Index

| Index | Type | Index | Type |
|-------|------|-------|------|
| 0 | motorway | 8 | residential |
| 1 | motorway_link | 9 | service |
| 2 | trunk | 10 | track |
| 3 | trunk_link | 11 | footway |
| 4 | primary | 12 | cycleway |
| 5 | primary_link | 13 | path |
| 6 | secondary | 14 | pedestrian |
| 7 | tertiary | 15 | tertiary_link |
| 255 | (custom) | — | uint8 len + UTF-8 follows |

### Surface Index

| Index | Surface | Index | Surface |
|-------|---------|-------|---------|
| 0 | paved | 4 | gravel |
| 1 | asphalt | 5 | dirt |
| 2 | concrete | 6 | sand |
| 3 | unpaved | 7 | grass |
| 255 | (custom) | — | uint8 len + UTF-8 follows |

### Query: "What road am I near?"

1. Convert query lat/lng → H3 cell (resolution 7)
2. Binary search index for cell
3. Fetch + decompress block
4. For each road segment, compute minimum distance from query point to linestring
5. Return nearest segment within threshold (default 50 m)
6. For points near cell edges: also check neighboring H3 cells (6 neighbors at res 7)

**Distance calculation** — point to linestring segment:

```python
def point_to_segment_distance(px, py, x1, y1, x2, y2):
    """Distance from point (px,py) to line segment (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

def point_to_linestring_distance(px, py, coords):
    """Minimum distance from point to any segment of the linestring."""
    return min(
        point_to_segment_distance(px, py, *coords[i], *coords[i+1])
        for i in range(len(coords) - 1)
    )
```

**Note:** Distances are in microdegrees. At mid-latitudes, multiply by ~0.9 m/microdegree for approximate meters. For precise results, use the Haversine formula.

### Estimated Size

| Metric | Value |
|--------|-------|
| OSM road segments (US) | ~20M |
| After cell splitting | ~28–30M |
| Avg vertices/segment | ~8–12 |
| Avg bytes/segment (compressed) | ~25–35 |
| **Total file size** | **~0.75–1.0 GB** |

---

## Layer: Admin Boundaries (A)

**Magic:** `PTILESA\x00`

Country, state, county boundaries plus ZIP codes and time zones. Uses a fundamentally different approach from per-cell feature layers — a **pre-computed H3 lookup grid** — because admin polygons are enormous (a state can have 50K+ vertices spanning millions of H3 cells).

### File Structure

```
┌─────────────────────────────────────────────────────────────────┐
│ Header (256 bytes)                                              │
├─────────────────────────────────────────────────────────────────┤
│ Zstd Dictionary                                                 │
├─────────────────────────────────────────────────────────────────┤
│ String Tables (state names, county names, ZIP codes, TZ names)  │
├─────────────────────────────────────────────────────────────────┤
│ Feature Table (boundary polygons — for rendering, not queries)  │
├─────────────────────────────────────────────────────────────────┤
│ H3 Lookup Grid (auxiliary section — the primary query structure) │
└─────────────────────────────────────────────────────────────────┘
```

The spatial index and data blocks sections of the header point to the string tables and feature table. The **auxiliary section** (`aux_offset`/`aux_length`) points to the H3 lookup grid.

### String Tables

Zstd-compressed arrays of null-terminated UTF-8 strings, referenced by index from the lookup grid.

| Table | Max entries | Example |
|-------|-------------|---------|
| Country names | uint8 (256) | "United States" |
| State names | uint8 (256) | "California" |
| County names | uint16 (65,536) | "San Francisco County" |
| ZIP codes | uint16 (65,536) | "94103" |
| Time zone names | uint8 (256) | "America/Los_Angeles" |

### H3 Lookup Grid

Sorted by H3 cell ID for binary search. One entry per H3 resolution 7 cell that intersects land.

```
┌──────────────────────────────────────────────────────────────┐
│ entry_count (4 bytes, uint32)                                │
├──────────────────────────────────────────────────────────────┤
│ Entry 0                                                      │
│   h3_cell        (8 bytes, uint64)                           │
│   country_idx    (1 byte,  uint8)  — index into country tbl  │
│   state_idx      (1 byte,  uint8)  — index into state tbl    │
│   county_idx     (2 bytes, uint16) — index into county tbl   │
│   zip_idx        (2 bytes, uint16) — index into ZIP tbl      │
│   tz_idx         (1 byte,  uint8)  — index into TZ tbl       │
│   boundary_flags (1 byte,  uint8)  — see below               │
├──────────────────────────────────────────────────────────────┤
│ Entry 1…N  (16 bytes each)                                   │
└──────────────────────────────────────────────────────────────┘
```

Entry size: **16 bytes** (8 + 1 + 1 + 2 + 2 + 1 + 1).

**Boundary flags:**

| Bit | Mask | Meaning |
|-----|------|---------|
| 0 | 0x01 | Cell straddles a state boundary |
| 1 | 0x02 | Cell straddles a county boundary |
| 2 | 0x04 | Cell straddles a ZIP code boundary |
| 3 | 0x08 | Cell straddles a time zone boundary |
| 4–7 | — | Reserved |

When a boundary flag is set, the lookup grid returns the **majority** region for that cell. For exact determination, fall back to point-in-polygon against the feature table polygons.

### Feature Table

Admin boundary polygons stored for rendering and PIP fallback. Each feature:

| Field | Encoding | Description |
|-------|----------|-------------|
| feature_id | uint16 | Unique ID |
| admin_level | uint8 | 2=country, 4=state, 6=county |
| name_len | uint16 | |
| name | UTF-8 | |
| vertex_count | uint32 | Can be very large |
| coordinates | int32 pairs | Absolute first + zigzag varint deltas |

### Query: "What state/county/ZIP am I in?"

```python
def query_admin(lat, lng, grid, string_tables, features):
    cell = h3.latlng_to_cell(lat, lng, 7)
    entry = binary_search(grid, cell)
    if entry is None:
        return None  # Ocean or outside coverage

    result = {
        "country": string_tables["country"][entry["country_idx"]],
        "state": string_tables["state"][entry["state_idx"]],
        "county": string_tables["county"][entry["county_idx"]],
        "zip": string_tables["zip"][entry["zip_idx"]],
        "timezone": string_tables["tz"][entry["tz_idx"]],
    }

    # If cell straddles a boundary, do precise PIP test
    if entry["boundary_flags"] & 0x01:
        # Fetch state polygons, test point-in-polygon
        result["state"] = pip_test(lat, lng, features, admin_level=4)

    return result
```

**Performance:** O(log n) binary search on ~380K entries. No decompression needed for the grid itself. Sub-millisecond.

### Estimated Size

| Component | Size |
|-----------|------|
| Lookup grid (380K × 16 bytes) | ~6 MB raw, ~2–3 MB compressed |
| String tables | ~200 KB |
| Boundary polygons | ~50–100 MB compressed |
| **Total** | **~50–100 MB** |

---

## Layer: Water (W)

**Magic:** `PTILESW\x00`

Water bodies (polygons) and waterways (linestrings) from OSM and NHD.

### Mixed Geometry Handling

| Feature type | Geometry | Storage strategy |
|--------------|----------|-----------------|
| Small pond/lake | Polygon | Per-cell block (like buildings) |
| Large lake/bay (> 1,000 vertices) | Polygon | Feature table + H3 cell references |
| River/stream/canal | LineString | Split at H3 boundaries (like roads) |

### Record Format

| Field | Encoding | Description |
|-------|----------|-------------|
| osm_id | varint (delta) | Delta from previous ID in block |
| geom_type | uint8 | 0 = polygon, 1 = linestring, 2 = reference |
| vertex_count | uint16 | Vertex count (0 if geom_type = 2) |
| first_lon | int32 | First longitude × 100,000 (absent if reference) |
| first_lat | int32 | First latitude × 100,000 (absent if reference) |
| deltas | zigzag varint pairs | (absent if reference) |
| [ref_feature_id] | uint32 | Only if geom_type = 2 — points to feature table |
| flags | uint8 | Bitmask for optional fields |
| water_type | uint8 | Indexed water type |
| [name] | uint16 len + UTF-8 | If flags & 0x01 |
| [width] | uint16 | If flags & 0x02 — width in decimeters (0–6,553.5 m) |
| [depth] | uint16 | If flags & 0x04 — max depth in decimeters |

**geom_type = 2 (reference):** For large water bodies. The record contains only a `ref_feature_id` pointing to a full polygon in the feature table. The record is duplicated in every H3 cell the water body covers, but only the ID (4 bytes) is repeated, not the geometry.

### Water Type Index

| Index | Type | Index | Type |
|-------|------|-------|------|
| 0 | lake | 7 | drain |
| 1 | reservoir | 8 | bay |
| 2 | pond | 9 | ocean |
| 3 | river | 10 | wetland |
| 4 | stream | 11 | marsh |
| 5 | creek | 12 | swamp |
| 6 | canal | 13 | estuary |
| 255 | (custom) | — | uint8 len + UTF-8 follows |

### Feature Table (Large Water Bodies)

Stored in the auxiliary section. Contains full polygon geometry for water bodies too large for per-cell storage.

| Field | Encoding | Description |
|-------|----------|-------------|
| feature_id | uint32 | Referenced by cell records |
| name_len | uint16 | |
| name | UTF-8 | |
| water_type | uint8 | |
| vertex_count | uint32 | |
| coordinates | int32 first + zigzag varint deltas | |

### Query: "Am I near water?"

1. lat/lng → H3 cell → fetch block
2. For polygons: point-in-polygon test
3. For linestrings: point-to-linestring distance
4. For references: fetch large feature from feature table, then PIP test
5. Return nearest/containing water feature within threshold

### Estimated Size

| Metric | Value |
|--------|-------|
| Water polygons (US, OSM + NHD) | ~3M |
| Waterway linestrings | ~2M |
| Large water body features | ~5K |
| **Total file size** | **~200–400 MB** |

---

## Layer: Places (P)

**Magic:** `PTILESP\x00`

Named populated places and neighborhoods — point features with name, type, and population.

### Record Format

| Field | Encoding | Description |
|-------|----------|-------------|
| osm_id | varint (delta) | Delta from previous ID in block |
| lon | int32 | Center longitude × 100,000 |
| lat | int32 | Center latitude × 100,000 |
| place_type | uint8 | Indexed place type |
| population | varint | Population (0 = unknown) |
| name_len | uint16 | |
| name | UTF-8 | Place name |
| flags | uint8 | Bitmask for optional fields |
| [alt_name] | uint16 len + UTF-8 | If flags & 0x01 — alternate/local name |
| [admin_level] | uint8 | If flags & 0x02 — OSM admin_level value |

### Place Type Index

| Index | Type | Typical population |
|-------|------|--------------------|
| 0 | city | > 100,000 |
| 1 | town | 10,000–100,000 |
| 2 | village | 1,000–10,000 |
| 3 | hamlet | < 1,000 |
| 4 | neighborhood | — |
| 5 | suburb | — |
| 6 | borough | — |
| 7 | quarter | — |
| 8 | isolated_dwelling | < 10 |
| 255 | (custom) | uint8 len + UTF-8 follows |

### Query: "What place/neighborhood am I in?"

1. lat/lng → H3 cell + neighboring cells (place influence extends beyond cell)
2. Collect all place records from these cells
3. Score by: distance to place center × place type weight (cities have wider influence)
4. Return the most specific applicable place (prefer neighborhood over city if within range)

**Scoring heuristic:**

| Place type | Influence radius |
|------------|-----------------|
| city | ~15 km |
| town | ~5 km |
| village | ~2 km |
| hamlet | ~500 m |
| neighborhood | ~1 km |
| suburb | ~3 km |

### Estimated Size

| Metric | Value |
|--------|-------|
| US populated places (OSM + Census) | ~200K |
| US neighborhoods | ~50K |
| Avg bytes/record (compressed) | ~40–60 |
| **Total** | **~20–50 MB** |

---

## Layer: Parks (N)

**Magic:** `PTILESN\x00`

National parks, state parks, forests, wildlife refuges, wilderness areas, and local parks. Uses the same **H3 lookup grid** approach as admin boundaries — most H3 cells are either entirely inside a park or entirely outside.

### File Structure

Same hybrid structure as admin boundaries:

```
Header → String Table → Feature Table → H3 Lookup Grid (auxiliary section)
```

### H3 Lookup Grid Entry

Many cells have no park. Only cells intersecting a protected area get entries. Cells can have **multiple** overlapping designations (e.g., a wilderness area within a national forest).

```
┌──────────────────────────────────────────────────────────────┐
│ entry_count (4 bytes, uint32)                                │
├──────────────────────────────────────────────────────────────┤
│ Entry 0                                                      │
│   h3_cell        (8 bytes, uint64)                           │
│   park_count     (1 byte,  uint8)  — overlapping parks       │
│   park_entries[] (5 bytes each)    — repeated park_count ×   │
│     park_id        (2 bytes, uint16) — index into feature tbl│
│     designation    (1 byte,  uint8)  — indexed type          │
│     gap_status     (1 byte,  uint8)  — PAD-US GAP status 1-4 │
│     boundary_flag  (1 byte,  uint8)  — 1 = cell on boundary  │
├──────────────────────────────────────────────────────────────┤
│ Entry 1…N                                                    │
└──────────────────────────────────────────────────────────────┘
```

Entry size: **variable** — 9 bytes + 5 bytes per overlapping park. Most cells have 0 or 1 park.

### Designation Index

| Index | Designation | Index | Designation |
|-------|-------------|-------|-------------|
| 0 | National Park | 7 | National Monument |
| 1 | National Forest | 8 | BLM Land |
| 2 | National Wildlife Refuge | 9 | National Seashore/Lakeshore |
| 3 | State Park | 10 | National Recreation Area |
| 4 | State Forest | 11 | National Grassland |
| 5 | City/County Park | 12 | Military Installation |
| 6 | Wilderness Area | 13 | Tribal Land |
| 255 | (custom) | — | uint8 len + UTF-8 follows |

### GAP Status (PAD-US)

| Value | Protection level |
|-------|-----------------|
| 1 | Managed for biodiversity — disturbance events proceed or are mimicked |
| 2 | Managed for biodiversity — disturbance events suppressed |
| 3 | Managed for multiple uses — subject to extractive use |
| 4 | No known mandate for protection |

### Feature Table

| Field | Encoding | Description |
|-------|----------|-------------|
| park_id | uint16 | |
| name_len | uint16 | |
| name | UTF-8 | |
| designation | uint8 | |
| managing_agency_len | uint8 | |
| managing_agency | UTF-8 | e.g., "NPS", "USFS", "BLM" |
| area_km2 | uint32 | Area in square kilometers |
| vertex_count | uint32 | |
| coordinates | int32 first + zigzag varint deltas | Boundary polygon |

### Query: "Am I in a park?"

1. lat/lng → H3 cell → binary search lookup grid
2. If no entry: not in any park
3. If entry with boundary_flag = 0: definitively inside the listed park(s)
4. If boundary_flag = 1: PIP test against feature table polygon

### Estimated Size

| Metric | Value |
|--------|-------|
| Protected areas (PAD-US) | ~300K features |
| H3 cells in protected areas | ~100K |
| **Total** | **~30–80 MB** |

---

## Layer: Rail & Transit (T)

**Magic:** `PTILEST\x00`

Rail lines and transit stations. LineStrings split at H3 boundaries (like roads). Stations stored as point features (vertex_count = 1).

### Record Format

| Field | Encoding | Description |
|-------|----------|-------------|
| osm_id | varint (delta) | Delta from previous ID in block |
| vertex_count | uint16 | 1 for stations, > 1 for lines |
| first_lon | int32 | First (or only) longitude × 100,000 |
| first_lat | int32 | First (or only) latitude × 100,000 |
| deltas | zigzag varint pairs | (empty if vertex_count = 1) |
| flags | uint8 | Bitmask for optional fields |
| rail_type | uint8 | Indexed rail/station type |
| [name] | uint16 len + UTF-8 | If flags & 0x01 |
| [operator] | uint8 len + UTF-8 | If flags & 0x02 |
| [gauge] | uint16 | If flags & 0x04 — track gauge in mm (1435 = standard) |
| [electrified] | uint8 | If flags & 0x08 — 0=no, 1=contact_line, 2=rail, 3=yes |

### Rail Type Index

| Index | Type | Index | Type |
|-------|------|-------|------|
| 0 | rail | 6 | monorail |
| 1 | subway | 7 | funicular |
| 2 | tram | 8 | station |
| 3 | light_rail | 9 | halt |
| 4 | narrow_gauge | 10 | tram_stop |
| 5 | preserved | 11 | subway_entrance |
| 255 | (custom) | — | uint8 len + UTF-8 follows |

### Query: "What's the nearest rail/transit?"

Same approach as roads: point-to-linestring distance for lines, point-to-point distance for stations.

### Estimated Size

| Metric | Value |
|--------|-------|
| Rail lines (US, OSM) | ~500K segments after splitting |
| Stations | ~50K |
| **Total** | **~30–60 MB** |

---

## Layer: POIs (I)

**Magic:** `PTILESI\x00`

Point-of-interest features that don't have building footprints — standalone nodes in OSM like trailheads, viewpoints, fire hydrants, cell towers, etc.

**Note:** POIs that _do_ have building footprints are in the Buildings layer. This layer covers the rest.

### Record Format

| Field | Encoding | Description |
|-------|----------|-------------|
| osm_id | varint (delta) | Delta from previous ID in block |
| lon | int32 | Longitude × 100,000 |
| lat | int32 | Latitude × 100,000 |
| flags | uint8 | Bitmask for optional fields |
| poi_type | uint8 | Indexed POI type |
| [name] | uint16 len + UTF-8 | If flags & 0x01 |
| [category] | uint8 len + UTF-8 | If flags & 0x02 — sub-type detail |
| [phone] | uint8 len + UTF-8 | If flags & 0x04 |
| [website] | uint16 len + UTF-8 | If flags & 0x08 |
| [opening_hours] | uint8 len + UTF-8 | If flags & 0x10 |

### POI Type Index

| Index | Type | Index | Type |
|-------|------|-------|------|
| 0 | fuel | 10 | cell_tower |
| 1 | parking | 11 | fire_hydrant |
| 2 | atm | 12 | bench |
| 3 | restaurant | 13 | toilet |
| 4 | cafe | 14 | drinking_water |
| 5 | fast_food | 15 | post_box |
| 6 | bank | 16 | recycling |
| 7 | pharmacy | 17 | charging_station |
| 8 | viewpoint | 18 | picnic_site |
| 9 | trailhead | 19 | campsite |
| 255 | (custom) | — | uint8 len + UTF-8 follows |

### Query: "What POIs are nearby?"

1. lat/lng → H3 cell (+ optional neighbors for wider radius)
2. Compute distance to each POI in cell
3. Return all within radius, sorted by distance

### Estimated Size

| Metric | Value |
|--------|-------|
| US POIs without buildings (OSM) | ~5–10M |
| Avg bytes/POI (compressed) | ~10–20 |
| **Total** | **~50–150 MB** |

---

## Layer: Address Ranges (D)

**Magic:** `PTILESD\x00`

Street-level address ranges for reverse geocoding. Each record is a road segment with address number ranges on the left and right sides.

**Source:** US Census TIGER/Line address range files — authoritative, complete US coverage, public domain.

### Record Format

| Field | Encoding | Description |
|-------|----------|-------------|
| tlid | varint (delta) | TIGER/Line feature ID, delta from previous |
| vertex_count | uint16 | Segment vertex count |
| first_lon | int32 | First longitude × 100,000 |
| first_lat | int32 | First latitude × 100,000 |
| deltas | zigzag varint pairs | Delta coordinates |
| flags | uint8 | Bitmask for optional fields |
| street_name_len | uint16 | |
| street_name | UTF-8 | Full street name (e.g., "N Main St") |
| [left_from] | varint | If flags & 0x01 — starting address, left side |
| [left_to] | varint | If flags & 0x01 — ending address, left side |
| [right_from] | varint | If flags & 0x02 — starting address, right side |
| [right_to] | varint | If flags & 0x02 — ending address, right side |
| [zip_left] | uint16 | If flags & 0x04 — ZIP code, left side |
| [zip_right] | uint16 | If flags & 0x08 — ZIP code, right side |
| [cfcc] | uint8 | If flags & 0x10 — Census Feature Class Code |

### Address Interpolation

```python
def reverse_geocode(lat, lng, segments):
    """Find nearest address for a GPS coordinate."""
    nearest = None
    min_dist = float("inf")

    for seg in segments:
        dist, t = point_to_linestring_with_parameter(lat, lng, seg["coords"])
        if dist < min_dist:
            min_dist = dist
            nearest = seg
            position = t  # 0.0 = start of segment, 1.0 = end

    if nearest is None or min_dist > 50:  # 50m threshold
        return None

    # Determine which side of the street (left vs right)
    side = classify_side(lat, lng, nearest["coords"], position)

    if side == "left" and "left_from" in nearest:
        addr_from, addr_to = nearest["left_from"], nearest["left_to"]
        zip_code = nearest.get("zip_left")
    elif side == "right" and "right_from" in nearest:
        addr_from, addr_to = nearest["right_from"], nearest["right_to"]
        zip_code = nearest.get("zip_right")
    else:
        return {"street": nearest["street_name"], "number": None}

    # Interpolate house number
    number = int(addr_from + position * (addr_to - addr_from))
    # Snap to odd/even based on side convention
    if addr_from % 2 != number % 2:
        number += 1

    return {
        "number": number,
        "street": nearest["street_name"],
        "zip": zip_code,
        "distance_m": min_dist,
        "formatted": f"~{number} {nearest['street_name']}"
    }
```

### Estimated Size

| Metric | Value |
|--------|-------|
| TIGER/Line address segments (US) | ~20M |
| After cell splitting | ~25–30M |
| Avg bytes/segment (compressed) | ~15–25 |
| **Total** | **~200–500 MB** |

## Layer: Routing (U)

**Magic:** `PTILESU\\x00`

See full spec at `docs/routing.md`.

Companion format alongside `.roads.ptiles` for offline point-to-point routing
via H3 portal stitching. Each H3 res-7 cell stores a precomputed distance
matrix between its boundary portal nodes, enabling fast frontier-expansion
routing without full-graph Dijkstra.

- **Input:** `{STATE}.roads.ptiles` (road segments with geometry, speed, oneway)
- **Output:** `{STATE}.routing.ptiles` (same bounds, same H3 cells)
- **Build:** `routing-index-builder TN.roads.ptiles TN.routing.ptiles`
- **Query engine:** PtilesRouter (Rust crate, JS library planned)
- **Algorithm:** H3 portal stitching (Duan et al. 2025)
- **Weight unit:** centiseconds (1/100 s), varint encoded
- **Zstd compressed:** per-cell blocks, no dictionary
- **Design doc:** `docs/offline-routing-prd.md`
- **DapStack:** ptil-16 (feature), ptil-17 (builder), ptil-18 (engine)

---

## Combined Query

Given any GPS coordinate, a multi-layer query reads across all files to build a complete location context:

```python
def query_all(lat, lng, layers):
    """Query all PTiles layers for a single GPS coordinate."""
    cell = h3.latlng_to_cell(lat, lng, 7)
    neighbors = h3.grid_disk(cell, 1)  # For proximity queries

    result = {}

    # Per-cell feature layers (parallel reads)
    result["building"] = query_building(lat, lng, cell, layers["buildings"])
    result["road"] = query_nearest_road(lat, lng, cell, neighbors, layers["roads"])
    result["water"] = query_water(lat, lng, cell, neighbors, layers["water"])
    result["rail"] = query_nearest_rail(lat, lng, cell, neighbors, layers["rail"])
    result["nearby_poi"] = query_pois(lat, lng, cell, neighbors, layers["poi"], radius_m=500)
    result["address"] = reverse_geocode(lat, lng, cell, neighbors, layers["addr"])

    # Lookup grid layers (instant)
    result["admin"] = query_admin(lat, lng, cell, layers["admin"])
    result["park"] = query_park(lat, lng, cell, layers["parks"])
    result["place"] = query_place(lat, lng, cell, neighbors, layers["places"])

    return result
```

### Example Output

```json
{
  "building": {
    "osm_id": 130905906,
    "name": "Starbucks",
    "type": "commercial",
    "height_m": 8.5
  },
  "road": {
    "name": "Main Street",
    "class": "secondary",
    "ref": null,
    "surface": "asphalt",
    "distance_m": 12
  },
  "address": {
    "formatted": "~1234 Main St",
    "zip": "94103",
    "distance_m": 8
  },
  "admin": {
    "country": "United States",
    "state": "California",
    "county": "San Francisco County",
    "zip": "94103",
    "timezone": "America/Los_Angeles"
  },
  "place": {
    "name": "Mission District",
    "type": "neighborhood",
    "city": "San Francisco"
  },
  "park": null,
  "water": {
    "name": "San Francisco Bay",
    "type": "bay",
    "distance_m": 1200
  },
  "rail": {
    "name": "BART",
    "type": "subway",
    "nearest_station": "16th St Mission",
    "distance_m": 200
  },
  "nearby_poi": [
    {"name": null, "type": "fuel", "distance_m": 85},
    {"name": "Dolores Park", "type": "viewpoint", "distance_m": 300}
  ]
}
```

### Storage Budget

| Layer | File | Est. Size |
|-------|------|-----------|
| Buildings | `US.ptiles` | ~1.14 GB |
| Roads | `US.roads.ptiles` | ~0.75–1.0 GB |
| Address Ranges | `US.addr.ptiles` | ~200–500 MB |
| Water | `US.water.ptiles` | ~200–400 MB |
| POIs | `US.poi.ptiles` | ~50–150 MB |
| Admin + ZIP + TZ | `US.admin.ptiles` | ~50–100 MB |
| Parks | `US.parks.ptiles` | ~30–80 MB |
| Rail/Transit | `US.rail.ptiles` | ~30–60 MB |
| Places | `US.places.ptiles` | ~20–50 MB |
| **Total** | | **~2.1–3.2 GB** |

### Query Performance

| Layer type | Seeks | Decompress | Computation |
|------------|-------|------------|-------------|
| Per-cell features | 1 range read | Yes (zstd) | Iterate records |
| Lookup grids | 1 range read | No | Binary search only |

All layers: **1 HTTP range request each** (after caching headers). Total query: ~9 range requests, ~50–500 KB transferred, sub-second total.

---

## Data Sources

| Layer | Primary Source | Secondary Source | License | Update Freq |
|-------|---------------|-----------------|---------|-------------|
| Buildings | OSM via Protomaps | — | ODbL | Continuous |
| Roads | OSM via Protomaps/Geofabrik | — | ODbL | Continuous |
| Admin Boundaries | US Census TIGER/Line | Natural Earth | Public domain | Annual |
| Water | NHD (Nat'l Hydrography) | OSM | Public domain / ODbL | Annual |
| Places | OSM | US Census Places | ODbL / Public domain | Continuous |
| Parks | USGS PAD-US | OSM leisure=park | Public domain / ODbL | Annual |
| Rail/Transit | OSM | — | ODbL | Continuous |
| POIs | OSM | Overture Maps | ODbL / CDLA | Continuous |
| Address Ranges | US Census TIGER/Line | — | Public domain | Annual |

### Available Cache

Pre-downloaded source data at `/Volumes/core/timeline-ptiles-cache/`:

| Directory | Contents | Size |
|-----------|----------|------|
| `raw/` | 47 state `.osm.pbf` files (full OSM extracts — roads, water, POIs, etc.) | ~10 GB |
| `buildings/` | Per-state `*-all-enriched.geojsonl` (building pipeline intermediate) | ~59 GB |
| `tiles-old/US.ptiles` | Final v6 building file | 1.1 GB |

### Known External Datasets

| Dataset | URL | Format |
|---------|-----|--------|
| US ZIP Code boundaries (2018) | `https://r2-public.protomaps.com/protomaps-sample-datasets/cb_2018_us_zcta510_500k.pmtiles` | PMTiles |

---

## Reference Decoders

### Shared Primitives

```python
import struct
import math

def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode unsigned varint. Returns (value, bytes_consumed)."""
    result = shift = 0
    start = pos
    while True:
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos - start

def zigzag_decode(n: int) -> int:
    """Decode zigzag-encoded unsigned integer to signed."""
    return (n >> 1) ^ -(n & 1)

def decode_coordinates(data: bytes, pos: int, first_lon: int, first_lat: int,
                       vertex_count: int) -> tuple[list, int]:
    """Decode delta-encoded coordinate sequence."""
    coords = [(first_lon / 100000, first_lat / 100000)]
    prev_lon, prev_lat = first_lon, first_lat
    start_pos = pos

    for _ in range(vertex_count - 1):
        dlon_raw, consumed = decode_varint(data, pos)
        pos += consumed
        dlat_raw, consumed = decode_varint(data, pos)
        pos += consumed

        prev_lon += zigzag_decode(dlon_raw)
        prev_lat += zigzag_decode(dlat_raw)
        coords.append((prev_lon / 100000, prev_lat / 100000))

    return coords, pos - start_pos

def decode_string(data: bytes, pos: int, len_bytes: int = 2) -> tuple[str, int]:
    """Decode length-prefixed UTF-8 string."""
    if len_bytes == 2:
        slen = struct.unpack_from("<H", data, pos)[0]
        pos += 2
    else:
        slen = data[pos]
        pos += 1
    s = data[pos:pos + slen].decode("utf-8")
    return s, 2 + slen if len_bytes == 2 else 1 + slen

def decode_indexed_string(data: bytes, pos: int, index: dict, idx_val: int) -> tuple[str, int]:
    """Decode indexed value or custom string if index is 255."""
    if idx_val == 255:
        s, consumed = decode_string(data, pos, len_bytes=1)
        return s, consumed
    return index.get(idx_val, "unknown"), 0
```

### Road Decoder

```python
ROAD_CLASS = {
    0: "motorway", 1: "motorway_link", 2: "trunk", 3: "trunk_link",
    4: "primary", 5: "primary_link", 6: "secondary", 7: "tertiary",
    8: "residential", 9: "service", 10: "track", 11: "footway",
    12: "cycleway", 13: "path", 14: "pedestrian", 15: "tertiary_link",
}

SURFACE = {
    0: "paved", 1: "asphalt", 2: "concrete", 3: "unpaved",
    4: "gravel", 5: "dirt", 6: "sand", 7: "grass",
}

def decode_road(data: bytes, offset: int, prev_osm_id: int = 0):
    """Decode a road segment record. Returns (road_dict, bytes_consumed)."""
    pos = offset

    # OSM way ID (delta varint)
    delta, consumed = decode_varint(data, pos)
    pos += consumed
    osm_id = prev_osm_id + delta

    # Vertex count (uint16)
    vertex_count = struct.unpack_from("<H", data, pos)[0]
    pos += 2

    # First coordinate
    first_lon, first_lat = struct.unpack_from("<ii", data, pos)
    pos += 8

    # Delta coordinates
    coords, consumed = decode_coordinates(data, pos, first_lon, first_lat, vertex_count)
    pos += consumed

    # Flags
    flags = data[pos]
    pos += 1

    # Road class
    rc_idx = data[pos]
    pos += 1
    road_class, consumed = decode_indexed_string(data, pos, ROAD_CLASS, rc_idx)
    pos += consumed

    road = {
        "osm_id": osm_id,
        "geometry": {"type": "LineString", "coordinates": coords},
        "road_class": road_class,
    }

    # Optional fields
    if flags & 0x01:
        road["name"], consumed = decode_string(data, pos, len_bytes=2)
        pos += consumed
    if flags & 0x02:
        road["ref"], consumed = decode_string(data, pos, len_bytes=1)
        pos += consumed
    if flags & 0x04:
        road["oneway"] = data[pos]
        pos += 1
    if flags & 0x08:
        road["speed_limit_kmh"] = data[pos]
        pos += 1
    if flags & 0x10:
        road["lanes"] = data[pos]
        pos += 1
    if flags & 0x20:
        sf_idx = data[pos]
        pos += 1
        road["surface"], consumed = decode_indexed_string(data, pos, SURFACE, sf_idx)
        pos += consumed
    if flags & 0x40:
        bt = data[pos]
        pos += 1
        road["bridge_tunnel"] = {0: None, 1: "bridge", 2: "tunnel"}.get(bt)

    return road, pos - offset

def decode_road_block(data: bytes) -> list[dict]:
    """Decode all road records from a decompressed block."""
    roads = []
    pos = 0
    prev_osm_id = 0
    while pos < len(data):
        record_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        road, _ = decode_road(data, pos, prev_osm_id)
        prev_osm_id = road["osm_id"]
        roads.append(road)
        pos += record_len
    return roads
```

### Admin Lookup Decoder

```python
def decode_admin_grid(data: bytes) -> list[dict]:
    """Decode the H3 admin lookup grid (auxiliary section)."""
    pos = 0
    entry_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4

    entries = []
    for _ in range(entry_count):
        h3_cell = struct.unpack_from("<Q", data, pos)[0]; pos += 8
        country = data[pos]; pos += 1
        state = data[pos]; pos += 1
        county = struct.unpack_from("<H", data, pos)[0]; pos += 2
        zip_idx = struct.unpack_from("<H", data, pos)[0]; pos += 2
        tz = data[pos]; pos += 1
        boundary_flags = data[pos]; pos += 1

        entries.append({
            "h3_cell": h3_cell,
            "country_idx": country,
            "state_idx": state,
            "county_idx": county,
            "zip_idx": zip_idx,
            "tz_idx": tz,
            "boundary_flags": boundary_flags,
        })

    return entries

def binary_search_admin(grid: list[dict], cell_int: int) -> dict | None:
    """Binary search the admin lookup grid."""
    left, right = 0, len(grid) - 1
    while left <= right:
        mid = (left + right) // 2
        mid_cell = grid[mid]["h3_cell"]
        if mid_cell == cell_int:
            return grid[mid]
        elif mid_cell < cell_int:
            left = mid + 1
        else:
            right = mid - 1
    return None
```

---

## Implementation Notes

### Shared Library Structure

All layers share encoding/decoding primitives. Recommended codebase organization:

```
ptiles/
  shared/
    header.py          # Header read/write
    varint.py          # Varint, zigzag encode/decode
    coordinates.py     # Coordinate delta encode/decode
    index.py           # Spatial index read/write + binary search
    compression.py     # Zstd dict training + compress/decompress
  layers/
    buildings.py       # Existing v6 encoder/decoder
    roads.py           # Road encoder/decoder + segment splitter
    admin.py           # Admin grid builder + decoder
    water.py           # Water encoder/decoder
    places.py          # Place name encoder/decoder
    parks.py           # Parks grid builder + decoder
    rail.py            # Rail encoder/decoder
    poi.py             # POI encoder/decoder
    addresses.py       # Address range encoder/decoder
  query.py             # Multi-layer combined query
  scripts/
    build_roads.py     # OSM → roads.ptiles pipeline
    build_admin.py     # TIGER + Natural Earth → admin.ptiles
    build_water.py     # NHD + OSM → water.ptiles
    build_places.py    # OSM + Census → places.ptiles
    build_parks.py     # PAD-US → parks.ptiles
    build_rail.py      # OSM → rail.ptiles
    build_poi.py       # OSM → poi.ptiles
    build_addr.py      # TIGER/Line → addr.ptiles
```

### Dependencies

| Library | Purpose | Layers |
|---------|---------|--------|
| h3 | H3 hexagonal indexing | All |
| zstandard | Dictionary compression | All |
| shapely | Point-in-polygon, line distance | Buildings, Water, Admin (fallback) |
| osmium (pyosmium) | Parse OSM PBF files | Roads, Water, Rail, POIs, Places |
| fiona/GDAL | Read shapefiles | Admin, Parks, Addresses |

### Build Order (Recommended)

1. **Roads** — most reuse from existing building pipeline
2. **Admin + ZIP + TZ** — small file, new lookup grid paradigm
3. **Places** — simple point features
4. **Water** — combines polygon + linestring + feature table patterns
5. **Parks** — reuses lookup grid from admin
6. **Address Ranges** — reuses road segment splitting
7. **Rail/Transit** — trivial variant of roads
8. **POIs** — trivial variant of places
