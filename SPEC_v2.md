# PTiles v2 Binary Format Specification

Performance-oriented revision of [SPEC.md](./SPEC.md) (v1). Same overall file layout (header -> dictionary -> index -> blocks); everything about how blocks are organized, indexed, and encoded has been redesigned to eliminate the per-cell zstd overhead that dominates v1 (85% of v1 blocks are under 1KB).

**Compat:** v2 readers detect format via `header.version` (>= 2). v1 files use v1 code paths; v2 files use v2 code paths. Same magic bytes.

---

## Table of Contents

1. [Block merging](#1-block-merging-v2-core)
2. [u16 coordinates relative to cell origin](#2-u16-coordinates-relative-to-cell-origin)
3. [Block bounding boxes in index](#3-block-bounding-boxes-in-index)
4. [Attribute chunking (v2.1)](#4-attribute-chunking-v21-extension)
5. [Sorted array index](#5-sorted-array-index)
6. [Feature-type histogram (optional)](#6-feature-type-histogram-optional)
7. [Record offset table](#7-record-offset-table)
8. [Routing graph format](#8-routing-graph-format-ptilesu)
9. [Versioning and backward compat](#9-versioning-and-backward-compat)
10. [Writer pseudocode](#10-writer-pseudocode)

---

## 1. Block merging (v2 core)

**Problem.** In v1, every H3 res-7 cell gets its own zstd-compressed block. 85% of blocks are < 1KB, so per-block framing/dictionary overhead dominates and decompression CPU is wasted on tiny payloads.

**Fix.** Adjacent low-density cells share one compressed block. The index still resolves at H3 res 7 (same spatial precision), but multiple cells map into the same block. Cells with >= 100 features remain solo.

### Merge policy

| Condition | Block kind |
|-----------|-----------|
| feature_count < 100 | candidate for merging |
| feature_count >= 100 | solo block (no merge) |
| Target decompressed size | 8-16 KB |
| Merge ordering | sorted by H3 cell ID (spatial locality) |

A merge group is closed when adding the next cell would push decompressed size above 16 KB, or a dense (>= 100) cell breaks the run.

### Merged block internal format (post-decompression)

```
+--------------------------------------------------------------+
| u32   cell_count                                             |
+--------------------------------------------------------------+
| u8    chunking_mode             (v2.1; 0 in baseline v2)     |
+--------------------------------------------------------------+
| i32   center_lon_micro          (cell-group centroid)        |
| i32   center_lat_micro                                       |
+--------------------------------------------------------------+
| u64   cell_ids[cell_count]                                   |
+--------------------------------------------------------------+
| u32   record_offsets[cell_count + 1]   (per-cell byte spans) |
+--------------------------------------------------------------+
| u8    record_data[...]   (v1 per-record encoding)            |
+--------------------------------------------------------------+
```

Records for `cell_ids[i]` span bytes `[record_offsets[i], record_offsets[i+1])` of `record_data`. Inside that range the format is the v1 layout (`u32 record_len + record_body` repeated). **Existing per-record decoders work unchanged**; only the block-reading layer changes.

### Solo block

A solo block is just a merged block with `cell_count == 1`. No special case.

---

## 2. u16 coordinates relative to cell origin

v1 stores absolute `i32` lon/lat (8 bytes for the first vertex, zigzag varint deltas after). v2 anchors every block to its centroid and stores `u16` offsets.

### Per-block header

```
i32  center_lon_micro     // cell-group centroid * 100,000
i32  center_lat_micro
```

### Per-vertex encoding

| Vertex position | Encoding |
|-----------------|----------|
| First vertex | `u16 delta_x`, `u16 delta_y` (bias 32768, absolute from center) |
| Subsequent | zigzag varint deltas from previous vertex |

```
delta_x = (lon_micro - center_lon_micro) + 32768
delta_y = (lat_micro - center_lat_micro) + 32768
```

### Range and precision

| Property | Value |
|----------|-------|
| u16 range (0-65535) | +/- 32768 microdegrees |
| Coverage from center | +/- 0.33 deg = +/- 36 km at equator |
| H3 res 7 cell diameter | ~2.3 km |
| Precision (1 microdeg) | ~0.1 m at equator |

A res-7 cell plus its 6 neighbors fits inside +/- 5 km, well under the +/- 36 km u16 range. Saves 4 bytes per feature on the first vertex.

v1 files retain `i32` encoding. The choice is implied by `header.version`.

---

## 3. Block bounding boxes in index

Each index entry gains a precomputed bbox so the reader can skip blocks that don't intersect the query rectangle without decompressing them.

### v2 index entry layout (37 bytes)

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0 | 8 | `h3_cell` (u64) | Primary key, sorted |
| 8 | 4 | `min_lon_micro` (i32) | NEW - block bbox |
| 12 | 4 | `min_lat_micro` (i32) | NEW |
| 16 | 4 | `max_lon_micro` (i32) | NEW |
| 20 | 4 | `max_lat_micro` (i32) | NEW |
| 24 | 6 | `block_offset` (u48) | Absolute file offset |
| 30 | 3 | `block_length` (u24) | Compressed size |
| 33 | 2 | `feature_count` (u16) | Features in this cell |
| 35 | 2 | `cell_index_in_block` (u16) | NEW - 0..cell_count-1 |

Total: **37 bytes** (was 19 in v1; delta = +18: 16 bbox + 2 cell_index).

When `cell_index_in_block == 0` and the merged block has `cell_count == 1`, it's a solo block.

### Query overlap test

```
overlaps =  query_max_lat >= entry_min_lat
         && query_min_lat <= entry_max_lat
         && query_max_lon >= entry_min_lon
         && query_min_lon <= entry_max_lon
```

Entries whose bbox doesn't overlap the query rect are skipped before any block fetch / decompression.

---

## 4. Attribute chunking (v2.1 extension)

Optional per-block split between geometry and attribute bytes. Detected by the `chunking_mode` byte after `cell_count`.

| `chunking_mode` | Meaning |
|-----------------|---------|
| 0 | Monolithic (single sub-frame, all records) |
| 1 | Geometry + attributes split into two sub-frames |

### Split layout

```
+----------------------------------------------+
| u32   geometry_length                        |
| u8[]  geometry_data        (zstd sub-frame)  |
+----------------------------------------------+
| u32   attributes_length                      |
| u8[]  attributes_data      (zstd sub-frame)  |
+----------------------------------------------+
```

`geometry_data` decompresses to coordinate-only records (u16 or v1 encoding per the block header). `attributes_data` decompresses to attribute-only records matching v1 record shape minus the coordinate bytes.

### Reader field filter

| Call | Decompresses |
|------|--------------|
| `read_block(cell, &["geometry"])` | geometry sub-frame only |
| `read_block(cell, &["attributes"])` | attributes sub-frame only |
| `read_block(cell, &["geometry","attributes"])` | both |

Skipping the attribute sub-frame is a 30-60% read-time win for proximity queries that don't need names/refs.

---

## 5. Sorted array index

The v2 spatial index is **sorted by H3 cell ID** (enforced by the builder). Readers MUST binary-search the array - no HashMap is built at open time.

| Property | v1 | v2 |
|----------|----|----|
| In-memory structure | `HashMap<u64, IndexEntry>` | `Vec<IndexEntry>` (sorted) |
| Open-time cost | O(n) hash insertions | O(1) (just mmap) |
| Lookup cost | O(1) avg | O(log n), cache-friendly |
| Memory | ~3x entry size | 1x entry size |

No format change - this is a builder-invariant + reader contract. The index entries already happen to be sorted in well-formed v1 files; v2 makes it a hard requirement.

---

## 6. Feature-type histogram (optional)

Per index entry, an optional bitmask of feature subtypes present in the block. Lets readers skip blocks that can't contain the type they're filtering for (e.g., "highways only", "restaurants only").

Layer-specific layout:

| Layer | Bytes | Encoding |
|-------|-------|----------|
| Roads | 2 | `u16 road_class_bits` - bit N set iff `road_class[N]` present (16 classes) |
| Business | 3 | `u8 top_category_id` + `u16 presence_bits` |
| Buildings | 3 | `u8 top_building_type` + `u16 presence_bits` |

### Presence signal in the index

The high bit of `header.index_length` flags histogram presence. When set, the per-entry histogram bytes immediately follow the bbox fields and precede `block_offset`. Readers MUST tolerate unknown flag bits (warn-and-skip).

---

## 7. Record offset table

Within a merged block's decompressed data, a flat offset array enables O(1) access to any record by global index across all cells in the block.

### Layout (inside `record_data`)

```
+----------------------------------------------+
| u32   total_record_count                     |
| u32   record_offsets[total_record_count + 1] |
| u8    record_bodies[...]                     |
+----------------------------------------------+
```

Record `k` spans `[record_offsets[k], record_offsets[k+1])`. The existing per-cell `record_offsets[cell_count+1]` (from section 1) addresses the per-cell ranges; this finer-grained table addresses individual records.

For solo blocks (`cell_count == 1`) with a single record group, the inner record-offset table is **optional** - readers fall back to sequential scan using `record_length` prefixes (v1-style).

---

## 8. Routing graph format (PTILESU)

New layer `.routing.ptiles`. Standard 256-byte header with magic `PTILESU\0` and `version >= 2`. No spatial index; the routing graph is one data blob loaded at open time.

### Data section layout

```
+-----------------------------------------------+
| u32   node_count                              |
| u32   edge_count                              |
+-----------------------------------------------+
| Node table  (node_count entries, 18 bytes ea) |
|   i32  lon_micro                              |
|   i32  lat_micro                              |
|   u32  edge_start_index                       |
|   u16  edge_count                             |
+-----------------------------------------------+
| Edge table  (edge_count entries, 14 bytes ea) |
|   u32  target_node                            |
|   u16  weight_driving        (centiseconds)   |
|   u16  weight_walking                         |
|   u16  weight_cycling                         |
+-----------------------------------------------+
```

(Node table is 14 bytes computed, padded to 18 for alignment - lon+lat+edge_start+edge_count = 4+4+4+2 = 14.)

### Size estimate (Tennessee)

| Component | Count | Bytes |
|-----------|-------|-------|
| Nodes | ~500K | 500K * 18 = 9 MB |
| Edges | ~1M | 1M * 14 = 14 MB |
| **Total** | | **~23 MB** |

CONUS scales roughly to ~2 GB. The blob is mmap-friendly and queries hit it directly without decompression.

---

## 9. Versioning and backward compat

| `header.version` | Reader path |
|------------------|-------------|
| < 2 | v1: HashMap index, per-cell blocks, i32 coords, no bbox |
| >= 2 | v2: binary-search array index, merged blocks, u16 coords, bbox |

Rules:

- v2 files share magic bytes with v1; only `version` distinguishes them.
- v1 index format (19-byte entries) is **untouched** for back-compat. v2 readers MUST be able to read v1 files.
- v2 writers MUST set `version = 2` and emit 37-byte index entries.
- Per-record encoding inside a block is **unchanged** from v1 (with the coord-encoding swap noted in section 2).
- Unknown flag bits / unknown optional sections are **skip-with-warning**, not reject. Forward-compat for v2.x extensions (e.g., section 4, 6).

---

## 10. Writer pseudocode

Reference Python flow using `shared.py` API patterns (`write_header`, `encode_index_entry`, `compress_block`).

```python
def build_v2_file(layer_data, out_path, compression_level=9, merge_threshold=100):
    # 1. Group features by H3 res-7 cell
    cells = group_by_h3_cell(layer_data)  # {h3_cell: [features]}

    # 2. Walk cells in sorted H3 order, merge sparse runs
    blocks = []        # list of (cell_ids, features_per_cell, bbox, center)
    pending = []       # list of (cell_id, features) being accumulated
    pending_size = 0

    for cell_id in sorted(cells):
        feats = cells[cell_id]
        if len(feats) >= merge_threshold:
            if pending:
                blocks.append(finalize_merged(pending))
                pending, pending_size = [], 0
            blocks.append(finalize_solo(cell_id, feats))
        else:
            est = estimate_decompressed_size(feats)
            if pending_size + est > 16 * 1024 and pending:
                blocks.append(finalize_merged(pending))
                pending, pending_size = [], 0
            pending.append((cell_id, feats))
            pending_size += est
    if pending:
        blocks.append(finalize_merged(pending))

    # 3. Encode + compress each block; build index entries
    index_entries = []
    block_payloads = []
    for blk in blocks:
        center = compute_centroid(blk.cells)
        raw = encode_merged_block(blk, center)         # section 1 + 2 + 7
        compressed = zstd.compress(raw, level=compression_level)
        block_offset = total_size_so_far  # tracked across iteration
        for i, cell_id in enumerate(blk.cells):
            index_entries.append(encode_index_entry(
                h3_cell        = cell_id,
                bbox           = blk.per_cell_bbox[i],     # section 3
                block_offset   = block_offset,
                block_length   = len(compressed),
                feature_count  = blk.per_cell_count[i],
                cell_index     = i,                        # section 1
            ))
        block_payloads.append(compressed)

    # 4. Sorted index (already in H3 order from the loop)
    assert is_sorted_by_h3(index_entries)                  # section 5

    # 5. Write header (version=2), dictionary, index, blocks
    write_header(out_path, version=2, ...)
    write_dictionary(out_path, train_dict(sample_features))
    write_index(out_path, index_entries)                   # 37 bytes each
    write_blocks(out_path, block_payloads)
    if layer == "business":
        write_aux_categories(out_path)
```

### Key invariants

| Invariant | Enforced where |
|-----------|----------------|
| Index sorted by H3 cell ID | Builder + reader assertion |
| `cell_index_in_block` < merged block's `cell_count` | Builder |
| Block bbox covers every vertex in the block | Builder |
| `center +/- 32768 microdeg` covers every vertex | Builder (fall back to solo block + larger center if not) |
| Solo blocks have `cell_count == 1`, `cell_index_in_block == 0` | Builder |
| `header.version == 2` | Builder writes, reader dispatches |

### Out-of-scope for v2.0

- Per-record dictionary refinement (defer to v2.2)
- Streaming / appendable writes (v2 is still a one-shot build)
- Cross-layer shared index (separate feature, not format-level)
