# US.ptiles Schema v7

Binary format for offline GPS → building lookup with full polygon footprints.

**v7 introduces wall-segment encoding for improved angle precision.**

## Overview

Single file containing 77M+ US building footprints with names where available.
Expected size: ~1.29 GB (at ~17 bytes/building average with wall-segment encoding).

## File Structure

```
┌─────────────────────────────────────────────────────────────────┐
│ Header (256 bytes)                                              │
├─────────────────────────────────────────────────────────────────┤
│ Zstd Dictionary (512 KB typical)                                │
├─────────────────────────────────────────────────────────────────┤
│ Spatial Index (H3 cell → block offset/length)                   │
├─────────────────────────────────────────────────────────────────┤
│ Data Blocks (zstd compressed, one per H3 cell)                  │
└─────────────────────────────────────────────────────────────────┘
```

## Header (256 bytes)

| Offset | Size | Type   | Field         | Description                        |
|--------|------|--------|---------------|------------------------------------|
| 0      | 8    | bytes  | magic         | `PTILESF\x00` (F = footprints)     |
| 8      | 1    | uint8  | version       | 7                                  |
| 9      | 3    | -      | reserved      | Padding                            |
| 12     | 4    | float  | min_lat       | Bounding box south                 |
| 16     | 4    | float  | min_lon       | Bounding box west                  |
| 20     | 4    | float  | max_lat       | Bounding box north                 |
| 24     | 4    | float  | max_lon       | Bounding box east                  |
| 28     | 8    | uint64 | poi_count     | Total building count               |
| 36     | 4    | uint32 | block_count   | Number of H3 cell blocks           |
| 40     | 8    | uint64 | dict_offset   | Byte offset to dictionary          |
| 48     | 4    | uint32 | dict_length   | Dictionary size in bytes           |
| 52     | 8    | uint64 | index_offset  | Byte offset to spatial index       |
| 60     | 4    | uint32 | index_length  | Index size in bytes                |
| 64     | 8    | uint64 | blocks_offset | Byte offset to first data block    |
| 72     | 184  | -      | reserved      | Future use                         |

## Spatial Index

H3 resolution 7 cells (~5.16 km² average). Sorted by H3 cell ID for binary search.

```
┌──────────────────────────────────────────────────────────────┐
│ entry_count (4 bytes, uint32)                                │
├──────────────────────────────────────────────────────────────┤
│ Entry 0                                                      │
│   h3_cell (8 bytes, uint64) - H3 index as integer            │
│   block_offset (6 bytes) - Absolute byte offset to block     │
│   block_length (3 bytes) - Compressed block size             │
│   poi_count (2 bytes, uint16) - Buildings in this cell       │
├──────────────────────────────────────────────────────────────┤
│ Entry 1...N (19 bytes each)                                  │
└──────────────────────────────────────────────────────────────┘
```

Entry size: 19 bytes (8 + 6 + 3 + 2)

## Data Block

Each block is zstd compressed (level 22) with shared dictionary.
Contains all buildings whose centroid falls within the H3 cell.

Decompressed format:
```
┌──────────────────────────────────────────────────────────────┐
│ Record 0                                                     │
│   record_length (4 bytes, uint32) - Size of record data      │
│   record_data (variable) - Building record                   │
├──────────────────────────────────────────────────────────────┤
│ Record 1...N                                                 │
└──────────────────────────────────────────────────────────────┘
```

Buildings within a block are sorted by OSM ID for delta encoding.

## Building Record (Binary v7)

| Field        | Encoding                | Description                          |
|--------------|-------------------------|--------------------------------------|
| osm_id       | varint (delta)          | Delta from previous OSM ID in block  |
| vertex_count | uint8                   | Polygon vertex count (max 255)       |
| first_lon    | int32                   | First longitude × 100,000            |
| first_lat    | int32                   | First latitude × 100,000             |
| **walls**    | **uint8 pairs**         | **angle + length for each segment**  |
| flags        | uint8                   | Bit flags for optional fields        |
| btype_idx    | uint8                   | Building type (see table)            |
| [btype_str]  | uint8 len + UTF-8       | Only if btype_idx = 255              |
| [name]       | uint16 len + UTF-8      | Only if flags & 0x01                 |
| [category]   | uint8 len + UTF-8       | Only if flags & 0x02                 |
| [name_src]   | uint8 len + UTF-8       | Only if flags & 0x04                 |
| [poi_osm_id] | uint64                  | Only if flags & 0x08                 |

### Wall Segment Encoding (v7)

Each wall segment (from vertex N to vertex N+1) is encoded as 2 bytes:

| Byte | Field  | Encoding                                 |
|------|--------|------------------------------------------|
| 0    | angle  | 0-255 maps to 0°-360° (1.4° resolution)  |
| 1    | length | 0-255 maps to 0-51m (20cm steps)         |

**Encoding formulas:**
```
angle_byte = floor(bearing_degrees * 256 / 360) % 256
length_byte = min(255, floor(length_meters / 0.2))
```

**Decoding formulas:**
```
bearing_degrees = angle_byte * 360 / 256
length_meters = length_byte * 0.2
```

Walls >51m (0.61% of walls) are clamped to 255 (51m).

### Precision Comparison

| Metric           | v6 (coords)      | v7 (wall-segment) |
|------------------|------------------|-------------------|
| Angle precision  | ~6° error        | 1.4° max          |
| Length precision | ~1.1m            | 20cm              |
| File size (US)   | 1.14 GB          | ~1.29 GB          |
| Photo matching   | Poor             | Excellent         |

### Flags Byte

| Bit | Mask | Field Present      |
|-----|------|--------------------|
| 0   | 0x01 | name               |
| 1   | 0x02 | category           |
| 2   | 0x04 | name_source        |
| 3   | 0x08 | poi_osm_id         |

### Building Type Index

| Index | Type         | Index | Type         |
|-------|--------------|-------|--------------|
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
| 255   | (variable)   | -     | Custom string follows |

### Coordinate Encoding (v7)

**v7 uses wall-segment encoding (see above). v6 and earlier used delta coordinates:**

- First coordinate: Absolute as int32 microdegrees (× 100,000)
- v7: Subsequent vertices encoded as angle+length wall segments
- v6: Subsequent vertices as delta coords, zigzag + varint encoded

**Coordinate reconstruction from wall segments:**
```python
def decode_walls(first_lon, first_lat, walls):
    coords = [[first_lon / 100000, first_lat / 100000]]
    current_lon, current_lat = first_lon / 100000, first_lat / 100000
    lon_scale = math.cos(math.radians(current_lat))

    for angle_byte, length_byte in walls:
        bearing_rad = angle_byte * 2 * math.pi / 256
        length_m = length_byte * 0.2
        delta_lat = (length_m * math.cos(bearing_rad)) / 111320
        delta_lon = (length_m * math.sin(bearing_rad)) / (111320 * lon_scale)
        current_lat += delta_lat
        current_lon += delta_lon
        coords.append([current_lon, current_lat])
        lon_scale = math.cos(math.radians(current_lat))
    return coords
```

### OSM ID Delta Encoding

Buildings sorted by OSM ID within each block. First building stores full ID,
subsequent store delta from previous.

Example:
```
Building 1: OSM ID 130905906 → varint(130905906)
Building 2: OSM ID 130905912 → varint(6)
Building 3: OSM ID 130905915 → varint(3)
```

## Query Algorithm

1. Convert query lat/lng to H3 cell (resolution 7)
2. Binary search index for matching H3 cell
3. Fetch block at offset (HTTP range request or file seek)
4. Decompress with dictionary
5. Iterate buildings, accumulating OSM ID deltas
6. Point-in-polygon test against query point
7. Return first containing building (or nearest within 50m)

## HTTP Range Request Pattern

For hosted files, cache header + dict + index on client (~1 MB).
Each query requires 1 range request for the data block (~2-50 KB compressed).

```
GET /US.ptiles
Range: bytes=0-786432          # Header + dict + index (once)

GET /US.ptiles
Range: bytes=12345678-12348000 # Single block per query
```

## Expected Statistics (US.ptiles v7)

| Metric               | Value          |
|----------------------|----------------|
| Total buildings      | 77,068,235     |
| H3 cells             | 380,425        |
| File size            | ~1.29 GB       |
| Bytes per building   | ~17            |
| Compression          | zstd level 22  |
| Dictionary size      | 512 KB         |
| Coverage             | 50 states + DC + PR + USVI |
| Angle precision      | 1.4°           |
| Length precision     | 20cm           |

### Per-State Samples

| State         | Buildings   | Approx Block Size |
|---------------|-------------|-------------------|
| California    | 9,305,713   | ~140 MB          |
| Texas         | 6,107,934   | ~92 MB           |
| New York      | 4,447,406   | ~67 MB           |
| Tennessee     | 919,888     | ~14 MB           |
| Rhode Island  | 242,328     | ~3.6 MB          |

---

## Parser Reference Code

### Varint Decoding (Python)

```python
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
```

### Zigzag Decoding (Python)

```python
def zigzag_decode(n: int) -> int:
    """Decode unsigned zigzag to signed integer."""
    return (n >> 1) ^ -(n & 1)
```

### Header Parsing (Python)

```python
import struct

def parse_header(data: bytes) -> dict:
    assert data[:8] == b"PTILESF\x00", "Invalid magic"
    return {
        "version": data[8],
        "min_lat": struct.unpack_from("<f", data, 12)[0],
        "min_lon": struct.unpack_from("<f", data, 16)[0],
        "max_lat": struct.unpack_from("<f", data, 20)[0],
        "max_lon": struct.unpack_from("<f", data, 24)[0],
        "poi_count": struct.unpack_from("<Q", data, 28)[0],
        "block_count": struct.unpack_from("<I", data, 36)[0],
        "dict_offset": struct.unpack_from("<Q", data, 40)[0],
        "dict_length": struct.unpack_from("<I", data, 48)[0],
        "index_offset": struct.unpack_from("<Q", data, 52)[0],
        "index_length": struct.unpack_from("<I", data, 60)[0],
        "blocks_offset": struct.unpack_from("<Q", data, 64)[0],
    }
```

### Index Entry Parsing (Python)

```python
def parse_index_entry(data: bytes, offset: int) -> dict:
    h3_cell = struct.unpack_from("<Q", data, offset)[0]
    # 6-byte block offset (little-endian)
    block_offset = int.from_bytes(data[offset+8:offset+14], "little")
    # 3-byte block length (little-endian)
    block_length = int.from_bytes(data[offset+14:offset+17], "little")
    poi_count = struct.unpack_from("<H", data, offset+17)[0]
    return {
        "h3_cell": format(h3_cell, "x"),
        "block_offset": block_offset,
        "block_length": block_length,
        "poi_count": poi_count,
    }
```

### Rust Struct Definitions

```rust
#[repr(C, packed)]
pub struct PtilesHeader {
    pub magic: [u8; 8],        // "PTILESF\0"
    pub version: u8,
    pub reserved: [u8; 3],
    pub min_lat: f32,
    pub min_lon: f32,
    pub max_lat: f32,
    pub max_lon: f32,
    pub poi_count: u64,
    pub block_count: u32,
    pub dict_offset: u64,
    pub dict_length: u32,
    pub index_offset: u64,
    pub index_length: u32,
    pub blocks_offset: u64,
    pub reserved2: [u8; 184],
}

pub struct IndexEntry {
    pub h3_cell: u64,
    pub block_offset: u64,  // Stored as 6 bytes
    pub block_length: u32,  // Stored as 3 bytes
    pub poi_count: u16,
}
```

---

## Version History

| Version | Changes                                          |
|---------|--------------------------------------------------|
| 7       | Wall-segment encoding (angle+length) (current)   |
| 6       | Delta OSM IDs + zigzag varint coords             |
| 5       | Varint coords, full OSM IDs                      |
| 4       | Binary footprints, fixed-size coords             |
| 3       | JSON minimal format                              |
| 1-2     | POI points only (no polygons)                    |

---

## Reference Implementations

| Language | File                              | Notes                    |
|----------|-----------------------------------|--------------------------|
| Python   | `scripts/build_ptiles_footprints.py` | Writer (encoder)      |
| Python   | `scripts/read_ptiles_footprints.py`  | Reader (decoder)      |

### Dependencies

- **h3**: Hexagonal spatial indexing
- **zstandard**: Compression with trained dictionary
- **shapely**: Point-in-polygon tests (reader only)

---

## Machine-Readable Schema (JSON)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "PTiles v7 Header",
  "type": "object",
  "properties": {
    "magic": { "const": "PTILESF\\u0000", "description": "8-byte magic" },
    "version": { "const": 7 },
    "bounds": {
      "type": "object",
      "properties": {
        "min_lat": { "type": "number" },
        "min_lon": { "type": "number" },
        "max_lat": { "type": "number" },
        "max_lon": { "type": "number" }
      }
    },
    "poi_count": { "type": "integer", "minimum": 0 },
    "block_count": { "type": "integer", "minimum": 0 },
    "dict_offset": { "type": "integer", "minimum": 256 },
    "dict_length": { "type": "integer" },
    "index_offset": { "type": "integer" },
    "index_length": { "type": "integer" },
    "blocks_offset": { "type": "integer" }
  }
}
```

### Building Type Enum (JSON)

```json
{
  "building_types": {
    "0": "yes",
    "1": "house",
    "2": "residential",
    "3": "commercial",
    "4": "industrial",
    "5": "retail",
    "6": "garage",
    "7": "apartments",
    "8": "office",
    "9": "warehouse",
    "10": "shed",
    "11": "detached",
    "12": "terrace",
    "13": "school",
    "14": "church",
    "15": "hospital",
    "16": "hotel",
    "17": "roof",
    "18": "construction",
    "19": "barn",
    "255": "custom"
  }
}
```
