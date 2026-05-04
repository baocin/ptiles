# PTiles v7 Schema Specification

Binary format for offline timeline map data with 7 specialized layers: buildings, roads, places, admin boundaries, water, rail, and parks.

## Overview

PTiles v7 extends the v6 single-layer building format into a multi-layer geospatial container. Each layer stores a specific geometry type optimized for its use case:

| Layer | Index | Geometry   | Description                          | Vertex Count |
|-------|-------|------------|--------------------------------------|-------------|
| Buildings | 0 | Polygon    | Building footprints (v6 compatible)  | uint8 (255 max) |
| Roads     | 1 | Linestring | Road network                         | uint16 (65535 max) |
| Places    | 2 | Point      | Cities, towns, POIs                  | N/A |
| Admin     | 3 | Polygon    | Administrative boundaries            | uint16 (65535 max) |
| Water     | 4 | Polygon    | Water bodies (lakes, rivers, ocean)  | uint16 (65535 max) |
| Rail      | 5 | Linestring | Railway network                      | uint16 (65535 max) |
| Parks     | 6 | Polygon    | Parks, forests, protected areas      | uint16 (65535 max) |

Each layer reuses the v6 compression techniques: H3 spatial indexing, zstd dictionary compression, and delta coordinate encoding. Layer-appropriate metadata fields are defined per record type.

## Key Differences from v6

| Feature | v6 | v7 |
|---------|-----|-----|
| Magic bytes | `PTILESF\0` (Footprints) | `PTILES7\0` |
| Header size | 256 bytes | 512 bytes |
| Layers | 1 (buildings only) | 7 (buildings, roads, places, admin, water, rail, parks) |
| Layer directory | None | 7 x 32 = 224 bytes |
| Index entries | 19 bytes (h3 + offset + length + count) | 20 bytes (+1 byte layer_type prefix) |
| Index sorting | h3_cell only | (layer_type, h3_cell) |
| Blocks offset | In header | In header + per-layer via directory |
| Vertex count for non-building layers | N/A | uint16 (supports up to 65,535 vertices) |
| Place coordinates | N/A | Absolute int32 at 1,000,000 precision |

## File Structure

```
┌─────────────────────────────────────────────────────────────────┐
│ Header (512 bytes)                                               │
├─────────────────────────────────────────────────────────────────┤
│ Zstd Dictionary (512 KB typical)                                 │
├─────────────────────────────────────────────────────────────────┤
│ Layer Directory (7 x 32 = 224 bytes)                             │
├─────────────────────────────────────────────────────────────────┤
│ Spatial Index (H3 cell to block offset/length, per layer)        │
├─────────────────────────────────────────────────────────────────┤
│ Data Blocks (zstd compressed, one per H3 cell per layer)        │
└─────────────────────────────────────────────────────────────────┘
```

## Header (512 bytes)

Byte order: Little-endian throughout.

| Offset | Size | Type   | Field              | Description                        |
|--------|------|--------|--------------------|------------------------------------|
| 0      | 8    | bytes  | magic              | `PTILES7\0`                        |
| 8      | 1    | uint8  | version            | 7                                  |
| 9      | 3    | -      | reserved           | Padding for alignment              |
| 12     | 4    | float  | min_lat            | Bounding box south                 |
| 16     | 4    | float  | min_lon            | Bounding box west                  |
| 20     | 4    | float  | max_lat            | Bounding box north                 |
| 24     | 4    | float  | max_lon            | Bounding box east                  |
| 28     | 1    | uint8  | layer_count        | Number of layers (7)               |
| 29     | 3    | -      | reserved           | Padding                            |
| 32     | 8    | uint64 | total_poi_count    | Total features across all layers   |
| 40     | 8    | uint64 | dict_offset        | Byte offset to dictionary          |
| 48     | 4    | uint32 | dict_length        | Dictionary size in bytes           |
| 52     | 8    | uint64 | layer_dir_offset   | Byte offset to layer directory     |
| 60     | 4    | uint32 | layer_dir_length   | Layer directory size in bytes      |
| 64     | 8    | uint64 | index_offset       | Byte offset to spatial index       |
| 72     | 4    | uint32 | index_length       | Index size in bytes                |
| 76     | 8    | uint64 | blocks_offset      | Byte offset to first data block    |
| 84     | 428  | -      | reserved           | Future use (zeroed)                |

### Rust Implementation

```rust
use ptiles_v7::PtilesReader;

let reader = PtilesReader::open("US.ptiles.v7").unwrap();
let header = reader.header();
assert_eq!(header.version, 7);
assert_eq!(header.layer_count, 7);
println!("Total features: {}", header.total_poi_count);
```

## Layer Directory

Fixed-size entries, one per layer. 32 bytes each, 7 layers = 224 bytes total.
Layers MUST appear in order 0-6.

| Offset | Size | Type   | Field              | Description                          |
|--------|------|--------|--------------------|--------------------------------------|
| 0      | 1    | uint8  | layer_type         | 0=building, 1=road, 2=place, 3=admin, 4=water, 5=rail, 6=park |
| 1      | 1    | uint8  | geometry_type      | 0=point, 1=linestring, 2=polygon     |
| 2      | 2    | -      | reserved           | Padding                              |
| 4      | 4    | uint32 | index_count        | H3 cell entries for this layer       |
| 8      | 8    | uint64 | poi_count          | Total features in this layer         |
| 16     | 16   | bytes  | layer_name         | UTF-8 name (null-padded)             |

### Rust Types

```rust
pub enum LayerType {
    Buildings = 0,
    Roads = 1,
    Places = 2,
    Admin = 3,
    Water = 4,
    Rail = 5,
    Parks = 6,
}

pub enum GeometryType {
    Point = 0,
    Linestring = 1,
    Polygon = 2,
}
```

## Spatial Index

All layer indices are concatenated into a single index section. Layer directory entries provide the count of entries per layer, so the reader knows where each layer's index begins and ends.

Entry format (20 bytes each, extended from v6's 19 bytes with a layer_type prefix):

| Field        | Size | Type   | Description                              |
|--------------|------|--------|------------------------------------------|
| layer_type   | 1    | uint8  | Layer type (0-6)                         |
| h3_cell      | 8    | uint64 | H3 cell index as integer (resolution 7)  |
| block_offset | 6    | -      | Absolute byte offset to compressed block |
| block_length | 3    | -      | Compressed block size in bytes           |
| poi_count    | 2    | uint16 | Feature count in this block              |

- 6-byte offset: Supports files up to 281 TB (2^48 bytes)
- 3-byte length: Max compressed block size 16 MB (2^24 bytes)

### Binary Search

Entries are sorted by (layer_type, h3_cell). Binary search on the combined key.

```python
def find_block(layer_type: int, h3_cell: int, index: bytes, counts: list[int], offsets: list[int]) -> dict | None:
    """Binary search within a specific layer's index region."""
    start = offsets[layer_type]
    end = start + counts[layer_type] * 20

    left = start
    right = end - 20
    while left <= right:
        mid = left + (((right - left) // 20) // 2) * 20  # align to entry boundary
        entry_lt = index[mid]
        entry_h3 = struct.unpack_from('<Q', index, mid + 1)[0]
        key = (entry_lt << 64) | entry_h3
        target = (layer_type << 64) | h3_cell
        if key == target:
            block_off = int.from_bytes(index[mid+9:mid+15], 'little')
            block_len = int.from_bytes(index[mid+15:mid+18], 'little')
            poi_cnt = struct.unpack_from('<H', index, mid + 18)[0]
            return {"block_offset": block_off, "block_length": block_len, "poi_count": poi_cnt}
        elif key < target:
            left = mid + 20
        else:
            right = mid - 20
    return None
```

### Rust Implementation

```rust
// Binary search by combined (layer_type << 64) | h3_cell key
let entry = reader.index().find(LayerType::Buildings, h3_cell);
// Or query all layers for a cell
let features = reader.query_cell_all(h3_cell);
```

## Zstd Dictionary

A single shared dictionary is trained on a representative sample across all layers. Dictionary training samples ~2,000 features per layer. The dictionary captures common coordinate delta patterns, string prefixes (like "Saint ", "Mount ", "County "), and geometry patterns across all layer types.

## Data Blocks

Each block is zstd compressed with the shared dictionary. Decompressed format is a sequence of records:

```
┌──────────────────────────────────────────────────────────────┐
│ Record 0                                                     │
│   record_length (4 bytes, uint32) - Size of record data      │
│   record_data (variable) - Layer-specific record             │
├──────────────────────────────────────────────────────────────┤
│ Record 1...N                                                 │
└──────────────────────────────────────────────────────────────┘
```

Records within a block are sorted by OSM ID for delta encoding efficiency.

## Layer 0: Buildings (Polygon)

Record format identical to v6 building records for backward compatibility.

| Field        | Encoding           | Description                          |
|--------------|--------------------|--------------------------------------|
| osm_id       | varint (delta)     | Delta from previous OSM ID in block  |
| vertex_count | uint8              | Polygon vertex count (max 255)       |
| first_lon    | int32              | First longitude x 100,000            |
| first_lat    | int32              | First latitude x 100,000             |
| deltas       | varint pairs       | Zigzag-encoded delta lon/lat         |
| flags        | uint8              | Bit flags for optional fields        |
| btype_idx    | uint8              | Building type index (0-19 or 255)    |
| [btype_str]  | uint8 len + UTF-8  | Custom building type (idx=255 only)  |
| [name]       | uint16 len + UTF-8 | flags & 0x01                         |
| [category]   | uint8 len + UTF-8  | flags & 0x02                         |
| [name_src]   | uint8 len + UTF-8  | flags & 0x04                         |
| [poi_osm_id] | uint64             | flags & 0x08                         |
| [height]     | uint8              | flags & 0x10 (0.5m steps)            |

### Building Type Index

| Index | Type         | Index | Type         |
|-------|-------------|-------|-------------|
| 0     | yes          | 10    | shed         |
| 1     | house        | 11    | detached     |
| 2     | residential  | 12    | terrace      |
| 3     | commercial   | 13    | school       |
| 4     | industrial   | 14    | church       |
| 5     | retail       | 15    | hospital     |
| 6     | garage       | 16    | hotel        |
| 7     | apartments   | 17    | roof         |
| 8     | office       | 18    | construction |
| 9     | warehouse    | 19    | barn         |
| 255   | (custom)     | -     | uint8 len + UTF-8 follows |

### Building Flags

| Bit | Mask | Field       |
|-----|------|-------------|
| 0   | 0x01 | name        |
| 1   | 0x02 | category    |
| 2   | 0x04 | name_source |
| 3   | 0x08 | poi_osm_id  |
| 4   | 0x10 | height      |
| 5-7 | -    | reserved    |

## Layer 1: Roads (Linestring)

| Field        | Encoding           | Description                          |
|--------------|--------------------|--------------------------------------|
| osm_id       | varint (delta)     | Delta from previous OSM ID           |
| vertex_count | uint16             | Linestring vertex count              |
| first_lon    | int32              | First longitude x 100,000            |
| first_lat    | int32              | First latitude x 100,000             |
| deltas       | varint pairs       | Zigzag-encoded delta lon/lat         |
| flags        | uint8              | Bit flags for optional fields        |
| road_class   | uint8              | Road class index                     |
| [name]       | uint16 len + UTF-8 | flags & 0x01                         |
| [ref]        | uint8 len + UTF-8  | flags & 0x02 (route number)          |
| [maxspeed]   | uint8              | flags & 0x04 (km/h)                  |
| [oneway]     | uint8              | flags & 0x08 (0=no, 1=yes)           |
| [lanes]      | uint8              | flags & 0x10                         |
| [surface]    | uint8 len + UTF-8  | flags & 0x20                         |

### Road Class Index

| Index | Class          | Index | Class       |
|-------|---------------|-------|-------------|
| 0     | motorway       | 8     | tertiary    |
| 1     | trunk          | 9     | unclassified|
| 2     | primary        | 10    | residential |
| 3     | secondary      | 11    | service     |
| 4     | motorway_link  | 12    | track       |
| 5     | trunk_link     | 13    | pedestrian  |
| 6     | primary_link   | 14    | path        |
| 7     | secondary_link | 255   | (custom)    |

### Road Flags

| Bit | Mask | Field    |
|-----|------|----------|
| 0   | 0x01 | name     |
| 1   | 0x02 | ref      |
| 2   | 0x04 | maxspeed |
| 3   | 0x08 | oneway   |
| 4   | 0x10 | lanes    |
| 5   | 0x20 | surface  |
| 6-7 | -    | reserved |

## Layer 2: Places (Point)

Places use absolute (non-delta) coordinates at 10x higher precision.

| Field        | Encoding           | Description                          |
|--------------|--------------------|--------------------------------------|
| osm_id       | varint (delta)     | Delta from previous OSM ID           |
| lon          | int32              | Longitude x 1,000,000                |
| lat          | int32              | Latitude x 1,000,000                 |
| flags        | uint8              | Bit flags for optional fields        |
| place_type   | uint8              | Place type index                     |
| [name]       | uint16 len + UTF-8 | flags & 0x01                         |
| [population] | uint32             | flags & 0x02                         |
| [capital]    | uint8              | flags & 0x04 (admin_level if capital)|
| [wikidata]   | uint8 len + UTF-8  | flags & 0x08                         |

### Place Type Index

| Index | Type          | Index | Type          |
|-------|--------------|-------|--------------|
| 0     | city          | 8     | suburb        |
| 1     | town          | 9     | neighbourhood |
| 2     | village       | 10    | locality      |
| 3     | hamlet        | 11    | island        |
| 4     | county        | 12    | farm          |
| 5     | state         | 13    | continent     |
| 6     | country       | 14    | ocean         |
| 7     | region        | 255   | (custom)      |

## Layer 3: Admin Boundaries (Polygon)

| Field        | Encoding           | Description                          |
|--------------|--------------------|--------------------------------------|
| osm_id       | varint (delta)     | Delta from previous OSM ID           |
| vertex_count | uint16             | Polygon vertex count                 |
| first_lon    | int32              | First longitude x 100,000            |
| first_lat    | int32              | First latitude x 100,000             |
| deltas       | varint pairs       | Zigzag-encoded delta lon/lat         |
| flags        | uint8              | Bit flags for optional fields        |
| admin_level  | uint8              | OSM admin_level (2-10)               |
| [name]       | uint16 len + UTF-8 | flags & 0x01                         |
| [iso_code]   | uint8 len + UTF-8  | flags & 0x02 (ISO 3166)              |
| [wikidata]   | uint8 len + UTF-8  | flags & 0x04                         |

## Layer 4: Water (Polygon)

| Field        | Encoding           | Description                          |
|--------------|--------------------|--------------------------------------|
| osm_id       | varint (delta)     | Delta from previous OSM ID           |
| vertex_count | uint16             | Polygon vertex count                 |
| first_lon    | int32              | First longitude x 100,000            |
| first_lat    | int32              | First latitude x 100,000             |
| deltas       | varint pairs       | Zigzag-encoded delta lon/lat         |
| flags        | uint8              | Bit flags for optional fields        |
| water_type   | uint8              | Water type index                     |
| [name]       | uint16 len + UTF-8 | flags & 0x01                         |

### Water Type Index

| Index | Type        | Index | Type        |
|-------|------------|-------|------------|
| 0     | lake        | 7     | reservoir   |
| 1     | pond        | 8     | basin       |
| 2     | river       | 9     | canal       |
| 3     | stream      | 10    | bay         |
| 4     | ocean       | 11    | strait      |
| 5     | sea         | 12    | wetland     |
| 6     | riverbank   | 255   | (custom)    |

## Layer 5: Rail (Linestring)

| Field         | Encoding           | Description                          |
|---------------|--------------------|--------------------------------------|
| osm_id        | varint (delta)     | Delta from previous OSM ID           |
| vertex_count  | uint16             | Linestring vertex count              |
| first_lon     | int32              | First longitude x 100,000            |
| first_lat     | int32              | First latitude x 100,000             |
| deltas        | varint pairs       | Zigzag-encoded delta lon/lat         |
| flags         | uint8              | Bit flags for optional fields        |
| rail_type     | uint8              | Rail type index                      |
| [name]        | uint16 len + UTF-8 | flags & 0x01                         |
| [usage]       | uint8              | flags & 0x02 (0=main,1=branch,2=industrial,3=military,4=tourism) |
| [gauge]       | uint16             | flags & 0x04 (mm, 1435=standard)     |
| [electrified] | uint8              | flags & 0x08 (0=no,1=contact_line,2=rail) |
| [tracks]      | uint8              | flags & 0x10                         |

### Rail Type Index

| Index | Type          | Index | Type       |
|-------|--------------|-------|------------|
| 0     | rail          | 7     | subway     |
| 1     | light_rail    | 8     | monorail   |
| 2     | tram          | 9     | funicular  |
| 3     | narrow_gauge  | 10    | miniature  |
| 4     | preserved     | 11    | disused    |
| 5     | abandoned     | 12    | spur       |
| 6     | construction  | 255   | (custom)   |

## Layer 6: Parks (Polygon)

| Field        | Encoding           | Description                          |
|--------------|--------------------|--------------------------------------|
| osm_id       | varint (delta)     | Delta from previous OSM ID           |
| vertex_count | uint16             | Polygon vertex count                 |
| first_lon    | int32              | First longitude x 100,000            |
| first_lat    | int32              | First latitude x 100,000             |
| deltas       | varint pairs       | Zigzag-encoded delta lon/lat         |
| flags        | uint8              | Bit flags for optional fields        |
| leisure_type | uint8              | Park/leisure type index              |
| [name]       | uint16 len + UTF-8 | flags & 0x01                         |
| [operator]   | uint8 len + UTF-8  | flags & 0x02                         |
| [access]     | uint8              | flags & 0x04 (0=yes,1=permissive,2=private,3=customers) |
| [website]    | uint16 len + UTF-8 | flags & 0x08                         |

### Leisure Type Index

| Index | Type             | Index | Type        |
|-------|-----------------|-------|-------------|
| 0     | park             | 8     | garden      |
| 1     | nature_reserve   | 9     | golf_course |
| 2     | forest           | 10    | pitch       |
| 3     | wood             | 11    | playground  |
| 4     | common           | 12    | beach       |
| 5     | recreation_ground| 13    | meadow      |
| 6     | national_park    | 14    | campground  |
| 7     | protected_area   | 255   | (custom)    |

## Delta Coordinate Encoding

Same as v6: coordinates stored as microdegrees (degrees x 100,000), zigzag-encoded signed deltas packed as protobuf-style varints. All layers use this encoding.

Buildings use uint8 vertex_count (max 255 vertices). All other polygon/linestring layers use uint16 vertex_count (max 65,535 vertices) to accommodate administrative boundaries and coastlines.

### Encoding Pipeline

1. **Calculate delta** from previous vertex (in microdegrees):
   ```
   delta_lon = current_lon - previous_lon
   delta_lat = current_lat - previous_lat
   ```

2. **Zigzag encoding** converts signed integers to unsigned (small magnitudes map to small values):
   ```
   zigzag(n) = (n << 1) ^ (n >> 31)
   Examples: 0->0, -1->1, 1->2, -2->3, 2->4
   ```

3. **Varint encoding** (protobuf-style, 7 bits per byte, MSB = continuation):
   ```
   while value >= 0x80:
       emit(0x80 | (value & 0x7F))
       value >>= 7
   emit(value)
   ```

## Coordinate Precision

| Layer    | Precision       | Unit        | Equivalent at equator |
|----------|-----------------|-------------|-----------------------|
| Building | 100,000         | microdegree | ~1.1m                 |
| Road     | 100,000         | microdegree | ~1.1m                 |
| Place    | 1,000,000       | 0.1 udeg    | ~0.11m                |
| Admin    | 100,000         | microdegree | ~1.1m                 |
| Water    | 100,000         | microdegree | ~1.1m                 |
| Rail     | 100,000         | microdegree | ~1.1m                 |
| Park     | 100,000         | microdegree | ~1.1m                 |

Places use higher precision (x10) since point coordinates are absolute, not delta-encoded.

## Query Algorithm

1. Convert query lat/lng to H3 cell (resolution 7)
2. Determine target layer(s) from layer directory
3. Binary search index for matching (layer_type, h3_cell)
4. Fetch block at offset (HTTP range request or file seek)
5. Decompress with shared dictionary
6. Iterate records, accumulating OSM ID deltas
7. Reconstruct geometry from delta coordinates
8. For polygon layers: point-in-polygon test; for linestring layers: nearest segment; for point layers: nearest point
9. Return matching feature(s)

## HTTP Range Request Pattern

Cache header + dictionary + layer directory + index on client (~1-5 MB). Each query requires 1 range request per layer for the data block (~2-50 KB compressed per block).

```
# Initial load (cached)
GET /US.ptiles.v7
Range: bytes=0-1048576       # Header + dict + layer dir + index

# Per-layer query
GET /US.ptiles.v7
Range: bytes=12345678-12348000  # Single block
```

## Version History

| Version | Changes                                                     |
|---------|-------------------------------------------------------------|
| **7**   | **Multi-layer: buildings, roads, places, admin, water, rail, parks (this)** |
| 6       | Delta OSM IDs + zigzag varint coords for buildings          |
| 5       | Varint coords, full OSM IDs                                 |
| 4       | Binary footprints, fixed-size coords                        |
| 3       | JSON minimal format (gzip)                                  |
| 1-2     | POI points only (no polygons)                               |

## Reference Implementations

| Language | Crate / File          | Notes                    |
|----------|-----------------------|--------------------------|
| Rust     | `ptiles-v7/`          | Schema + reader library  |

### Dependencies

- **h3** (h3o): Hexagonal spatial indexing (Uber H3 library)
- **zstd**: Compression with trained dictionary
- **thiserror**: Error handling
- **byteorder**: Little-endian binary parsing
