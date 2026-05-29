# Write a complete v8 building record decoder in Rust

## Goal

Write a v8 `parse_record_v8()` decoder in `ptiles/src/buildings.rs` so the Rust ptiles CLI can query building footprints from v8 PTILES files. The index is fixed (7893 entries in TN file) but `parse_record()` doesn't understand v8 — reads the flags byte as vertex_count, uses i32 instead of i16 for first vertex, uses wall_segments instead of varint zigzag deltas. Result: `decompress_block()` returns 0 buildings.

The reference decoder is Python at `~/kino/projects/ptiles/scripts/encode_v8.py:decode_building_v8()` (line 240). The block format is documented in the header of that file.

## Concrete Deliverables

1. Add `parse_record_v8()` function to `buildings.rs`
2. Add `decode_string_table()` helper to `codec.rs` (uleb128 count + uleb128-length-prefixed strings)
3. Modify `decompress_block()` to detect v8 (version == 8) and: (a) decode the block's string table before iterating records, (b) call `parse_record_v8()` for each record passing the string table
4. `parse_record_v8()` needs the **cell center** in microdegrees — compute from `entry.h3_cell` via `h3o::CellIndex::try_from()` → `LatLng::from()` instead of storing it in the index
5. Update `Building` struct if needed for v8 fields (use_class, height_tier, height_m)

## Key v8 Record Format

Byte layout (after u32 record-length prefix):

| Bytes | Field | Type | Notes |
|-------|-------|------|-------|
| varint | osm_id_delta | u64 varint | zigzag delta from prev osm_id |
| 1 | flags | u8 | bits 0-1: use_class, 2-3: height_tier, 4-7: vc_packed |
| 1 (conditional) | vertex_raw | u8 | only if vc_packed == 0x0F |
| 4 | first_lon_offset | **i16** | cell-relative microdegrees (NOT i32!) |
| 4 | first_lat_offset | **i16** | cell-relative microdegrees |
| varint pairs | delta_lon, delta_lat | varint | zigzag delta from previous vertex. repeat vertex_count - 1 times |
| 1 | btype_idx | u8 | building type table index; 0xFF = inline follows |
| var (conditional) | btype_inline | u8 len + UTF-8 | only if btype_idx == 0xFF |
| 1 | flags2 | u8 | extended flags |
| optional | name | u8 table_ref | if flags2 & 0x01 |
| optional | category | u8 table_ref | if flags2 & 0x02 |
| optional | name_source | u8 table_ref | if flags2 & 0x04 |
| optional | poi_osm_id | u64 | if flags2 & 0x08 |
| optional | height_raw | u8 | if flags2 & 0x10, 0.5m/step, clamp max=127.5m |

### String Table Format

Before records in each decompressed block:
```
u8 entry_count
for each entry:
  u8 length
  UTF-8 bytes (length)
```

The string table is indexed by position (0, 1, 2, ...). A `table_ref` is a u8 index into this table. Value 0xFF means the next byte is a length, followed by that many UTF-8 bytes (inline string).

### Cell Center Conversion

i16 offsets are relative to the H3 cell center in microdegrees (degrees * 100_000). To convert to absolute coordinates:

```rust
let cell = h3o::CellIndex::try_from(entry.h3_cell)?;
let center = h3o::LatLng::from(cell);
let center_lon_micro = (center.lng() * 100_000.0) as i32;
let center_lat_micro = (center.lat() * 100_000.0) as i32;
let lon_abs = center_lon_micro + first_lon_offset as i32;
let lat_abs = center_lat_micro + first_lat_offset as i32;
```

### Vertex Count Decoding

```rust
let vc_packed = (flags >> 4) & 0x0F;
let vertex_count = if vc_packed == 0x0F {
    data[pos] as usize  // raw byte follows
} else {
    vc_packed as usize + 4
};
```

## Test Once Done

```bash
CARGO_TARGET_DIR=/tmp/ptiles-target cargo build -p ptiles 2>&1 | tail -3
# Point query
/tmp/ptiles-target/debug/ptiles ~/kino/projects/ptiles/data/states/TN.buildings_v8.ptiles 36.1627 -86.7816
# Expected: shows "Music City Center" or similar building
# Bounds query
/tmp/ptiles-target/debug/ptiles ~/kino/projects/ptiles/data/states/TN.buildings_v8.ptiles bounds 36.16 -86.79 36.17 -86.77 --json 2>&1 | tail -5
# Expected: FeatureCollection with building features
```

Then restart the MVP server and test the web UI click-highlight at localhost:9352.

## Files To Modify

- `~/kino/projects/timeline/ptiles/src/buildings.rs` — Add `parse_record_v8()`, modify `decompress_block()`, add string_table
- `~/kino/projects/timeline/ptiles/src/codec.rs` — Add `decode_string_table()` (or add to codec module)

## What NOT to do

- Do NOT change the PTILES file format or rebuild the Python builder
- Do NOT add cell_center to IndexEntry — recompute from h3_cell
- Do NOT modify the v6/v7 code path (keep `parse_record()` as-is for version < 8)
- Do NOT adjust Building struct fields unless they're needed for GeoJSON serialization in main.rs

## Done

- `parse_record_v8()` added to `buildings.rs` — handles v8 flags byte, i16 cell-relative offsets,
  varint zigzag vertex deltas, string-table-referenced building type, extended flags2 with
  optional fields (name, category, name_source, poi_osm_id, height_m)
- `decompress_block()` routes v8 to separate code path: decodes string table, passes cell center
  from h3o::LatLng::from(entry.h3_cell), calls `parse_record_v8()`
- `codec.rs`: added `decode_string_table()`, `decode_table_ref()`, `zigzag_decode_u64()`
- Verified: point query at (36.1627, -86.7816) returns building with 6 vertices
- Verified: bounds query returns 100+ named buildings (Ryman, Capitol, AT&T Tower, etc.)
- Verified: MVP API endpoint returns GeoJSON polygon coordinates
