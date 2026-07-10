# PTILES Packing — Full Enhancement Catalog

Already in v6: zigzag coord deltas, delta OSM IDs, indexed types (20), H3+zstd
Planned v8: height/use flags, RLE names, micro-bldg skip, multi-res H3

## Tier 1 — High Impact (10-20% each)

### 1. Per-Cell String Table
Problem: 50 "Walmart" buildings in one H3 cell = 50x the same string.
Fix: Small string table at block start. Each name reference = 1 byte index.
```
Block header:
  string_count: u8
  table[0..N]: u8_len + UTF-8

Record:
  name_idx: u8 (0xff = inline, u8_len + str follows)
```
Savings: ~15% on names. Simple to implement.

### 2. Cell-Relative Coordinates
Problem: TN is -90° to -81° lon, but we store full int32 microdegrees.
Fix: Store first vertex as offset from H3 cell centroid.
```
cell_center_lon = h3.cell_to_latlng(cell)[1]
offset_lon = coord_to_micro(lon) - coord_to_micro(cell_center_lon)
```
Delta from center is small (max ~2.5km at res7 → fits in i16).
First vertex drops from 8 bytes to 4. Delats unchanged.
Savings: ~10% on geometry. H3 cell center already known from index.

### 3. Shape Dictionary + Affine
Problem: 40% of buildings are simple rectangles. Encoding 4 corners costs 10+ bytes.
Fix: Store {4-corner rect, L-shape, T-shape} as 1-byte shape index + 3 params.
```
Shape 0: rectangle → width_delta, height_delta (2 varints)
Shape 1: L-shape   → cutout_x, cutout_y (2 varints)
...
Shape 255: raw polygon (fallback, full delta encoding)
```
Savings: 30-60% on shaped buildings. Adds decode complexity.

## Tier 2 — Medium Impact (5-10% each)

### 4. Hilbert Curve Sort (Morton Order)
Problem: OSM ID sort clusters by creation time, not geography.
Fix: Sort buildings within each H3 cell by Hilbert/Morton code.
Adjacent buildings → adjacent in block → smaller coord deltas.
Savings: 5-8% on coord deltas. No format change needed.

### 5. Vertex Count Bias
Problem: Vertex count stored as raw u8 (always 1 byte, usually 4-8).
Fix: Bias encoding. verts 4-11 = u4 (half byte). 12+ = u4=0xf + u8.
```
if 4 <= vc <= 11:  flags_bits = vc - 4  (3 bits, packed into flags)
else:              verts_raw = u8 follows
```
Savings: 4 bits/record avg. Fits in existing flags byte.

### 6. Building Type Runs
Problem: Residential blocks = 200x "house" in sequence.
Fix: Type run counter. When type doesn't change from prev, skip btype byte.
```
flags bit: type_changed? if same, btype_idx omitted
Or: run-length byte = "next N records all same type"
```
Savings: 1 byte/record in homogeneous blocks.

### 7. Coord Quantization
Problem: Microdegrees (1.1m) is finer than needed for small buildings.
Fix: Use decimicrodegrees (0.1m) or store as i16 from local origin.
Savings: 2 bytes/vertex. Small quality loss for sheds/garages.

## Tier 3 — Structural (format-level)

### 8. Op-code Encoding
Problem: Fixed record structure wastes bits on field presence.
Fix: Op-code byte selects record variant.
```
0x00-0x3F: Simple building (type+flags packed, 1 byte)
0x40-0x7F: Named building (+name index, 2-4 bytes)
0x80-0xBF: Detailed (+height, category, source, 4-8 bytes)
0xC0-0xFF: Full (+POI link, addr, phones, 8+ bytes)
```
Savings: 2-3 bytes/record for 80% of buildings (simple, no name).

### 9. Two-Pass Encoding
Problem: We can't RLE names without seeing the whole block first.
Fix: Pass 1 = collect stats, build string table, find patterns.
Pass 2 = encode with full knowledge of the block.
Savings: Unlocks all intra-block optimizations (RLE, dedup, runs).

### 10. Differential Block Encoding
Problem: Adjacent H3 cells have similar building patterns (same suburb).
Fix: Store some blocks as deltas from a reference block.
```
block_flags: 0=full, 1=delta_from(prev_block, offset=...)
```
Savings: 20-40% on homogeneous regions. High complexity.

## Tier 4 — Domain-Specific

### 11. Footprint Simplification
Already: simplify(0.00005). Could push to 0.0001 for residential.
Measures: Reduce verts by 30% for 50% of buildings.
Trade: 5m precision loss for houses (acceptable for map display).

### 12. Multi-Resolution H3 (already planned)
Problem: Dense cities have huge blocks (10k+ buildings), rural has tiny blocks.
Fix: res7 for rural, res8/9 for urban. Header switch per block.
Savings: ~15% on index size + better zstd locality.

### 13. Numeric-Only Address Prefix
When addr is "123 Main St" and prev was "122 Main St",
store only "3" as delta from prev housenum + "Main St" as index.
Domain-specific but very effective for street-level compression.

## Recommended v8 Implementation Order

1. Per-cell string table (high impact, easy)
2. Cell-relative first vertex (high impact, easy)
3. Vertex count bias (medium, very easy)
4. Two-pass encoding w/ type runs (medium, unlocks all RLE)
5. Hilbert sort (medium, needs new sort)
6. Shape dictionary (high, complex — v9?)

Target: 15 → 10 bytes/bldg (1.14GB → 770MB US)