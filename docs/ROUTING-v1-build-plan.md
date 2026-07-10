# Build Pipeline: `.routing.ptiles` Generator

Generate pre-built routing graph files from existing `.roads.ptiles`
data. Each state produces one `{STATE}.routing.ptiles` file using the
same graph-building logic already in `router.rs::build_graph`.

**Status: IMPLEMENTED** (June 20, 2026)

## Usage

```
ptiles build-routing <states_dir/state.roads.ptiles> [output_dir]
```

## What It Does

Reuses the existing `build_graph` function plus the existing `RoadsReader`
to load road segments. The graph output is serialized to the `.routing.ptiles`
format instead of being used for immediate routing.

## Implementation

### Module: `src/build_routing.rs`

Three public functions:

1. **`build_routing_file(roads_path, output_dir)`** — loads all road segments
   from a `.roads.ptiles` file, calls `build_graph` with profile="driving",
   serializes to `.routing.ptiles`.

2. **`serialize_routing_file(path, node_count, adjacency, micro_coords, roads_header)`**
   — writes the flat-array binary format:
   - 256-byte header (PTILES magic, bbox from source roads file, section offsets)
   - H3 node spatial index (24-byte entries: h3_cell, first_node, node_count, edge_count)
   - Node table (24-byte records: lat_micro, lon_micro, 16B placeholders for osm_id, road_class, flags)
   - Edge start index (4-byte cumulative offsets, N+1 entries)
   - Edge table (8-byte records: target u32, weight u16, flags u16)

3. **`build_node_spatial_index(micro_coords, node_count, adjacency)`** — groups
   nodes by H3 resolution 7 cell, computes node ranges and edge counts per cell.

### CLI: `main.rs`

The `build-routing` subcommand accepts a single `.roads.ptiles` file or a
directory of them. When a directory is given, all files matching `*.roads.ptiles`
are processed in batch.

### Module Wiring

`pub mod build_routing;` in `lib.rs`.

### Key Design Decisions

1. **No zstd dictionary.** Flat arrays don't benefit from dict compression
   (they're already dense, fixed-size records). The old spec doc had a zstd dict
   section; this was dropped. No dict means the file can be mmap'd directly.

2. **Profile = driving only.** v1 stores driving weights in centiseconds.
   Walking/cycling use speed multipliers at query time. The road_class bitmask
   and aux section (profile speed tables) are reserved for future v2.

3. **Weight clamped to u16.** Edge weights > 65535 cs (~11 min) are clamped.
   Longer edges than 11 min are vanishingly rare in road graphs (<0.1%).

4. **Bbox copied from source roads file.** The output routing file inherits
   the bounding box from its source `.roads.ptiles` header.

## Verification

1. Build a routing file for TN:
   ```
   ptiles build-routing data/states/TN.roads.ptiles data/states/
   ```
2. Check file size is in the expected range (5-8 MB for TN).
3. Load it in the existing router, route same origin/dest pair.
4. Assert same path, same distance ± small floating-point tolerance.

**Test file location:** `~/kino/projects/ptiles/data/states/TN.routing.ptiles`

## What's Missing (v1)

- **Node spatial index edge counts**: computed correctly (2-pass).
- **Profile-specific handling**: v1 stores driving only.
- **OSM way metadata**: not stored (no turn-by-turn).
- **Highways variant**: after generating per-state routing files, generate
  `US.highways.routing.ptiles` by filtering to highway-class edges only.
- **Cross-state node dedup**: not handled. At multi-state query time, the
  router merges nodes by proximity.
- **Aux section**: not written in v1. Profile speeds + road class map
  placeholders are zeroed.
- **Reader side**: the `.routing.ptiles` reader hasn't been implemented yet.
  The build step is done — consumption comes next.
