# PTILES Routing v1 — Pre-built Graph Format

A compact binary routing graph format for offline shortest-path queries.
Flat, fixed-size arrays — no compression, no per-cell blocks, directly
mmap-able. Stores a pre-built directed graph (node adjacency + edge costs)
so the router skips graph construction entirely.

## Status

| Component                            | Status                           |
| ------------------------------------ | -------------------------------- |
| Builder (`build-routing` subcommand) | IMPLEMENTED (June 20, 2026)      |
| Reader (A\* on flat arrays)          | NOT YET                          |
| Aux section (profile speeds)         | NOT YET (v1 stores driving only) |

## File Extension

`.routing.ptiles` — one per state.

## Magic

| Offset | Size | Value       | Description            |
| ------ | ---- | ----------- | ---------------------- |
| 0      | 7    | `PTILESR\0` | Magic bytes + null pad |
| 7      | 1    | `\x01`      | Version (1 for v1)     |

## File Layout

```
┌───────────────────────────────────────┐ 0
│ Header (256 bytes)                    │
├───────────────────────────────────────┤ header.node_index_offset
│ Node spatial index                    │
│ (H3 res 7 cell → node_id range)       │
├───────────────────────────────────────┤ header.nodes_offset
│ Node table (uncompressed flat array)  │
├───────────────────────────────────────┤ header.edge_index_offset
│ Edge start index                      │
│ (cumulative edge counts per node)     │
├───────────────────────────────────────┤ header.edges_offset
│ Edge table (uncompressed flat array)  │
└───────────────────────────────────────┘
```

No zstd dictionary, no per-cell blocks, no aux section in v1.
The file is a sequence of flat arrays that can be mmap'd and queried
with zero parsing overhead beyond the header.

## Header (256 bytes, little-endian)

| Offset | Size | Type  | Field             | Description                                        |
| ------ | ---- | ----- | ----------------- | -------------------------------------------------- |
| 0      | 7    | bytes | magic             | `PTILESR\0`                                        |
| 7      | 1    | u8    | version           | 1                                                  |
| 8-11   | 4    | -     | \_reserved        | Zeroed                                             |
| 12     | 4    | f32   | min_lat           | Bounding box south (copied from source roads file) |
| 16     | 4    | f32   | min_lon           | Bounding box west                                  |
| 20     | 4    | f32   | max_lat           | Bounding box north                                 |
| 24     | 4    | f32   | max_lon           | Bounding box east                                  |
| 28     | 4    | u32   | node_count        | Total nodes in graph                               |
| 32     | 4    | u32   | edge_count        | Total directed edges                               |
| 36     | 4    | u32   | profile_count     | 1 (driving in v1)                                  |
| 40     | 4    | u32   | road_class_count  | 16 (reserved)                                      |
| 44     | 8    | u64   | dict_offset       | 0 (no dict in routing files)                       |
| 48     | 4    | u32   | dict_length       | 0                                                  |
| 52     | 8    | u64   | node_index_offset | Byte offset to node spatial index                  |
| 60     | 4    | u32   | node_index_length | Node spatial index size (bytes)                    |
| 64     | 8    | u64   | nodes_offset      | Byte offset to node table                          |
| 72     | 4    | u32   | nodes_length      | Node table size (bytes)                            |
| 80     | 8    | u64   | edge_index_offset | Byte offset to edge start index                    |
| 88     | 4    | u32   | edge_index_length | Edge start index size (bytes)                      |
| 92     | 8    | u64   | edges_offset      | Byte offset to edge table                          |
| 100    | 4    | u32   | edges_length      | Edge table size (bytes)                            |
| 104    | 8    | u64   | aux_offset        | 0 (no aux section in v1)                           |
| 112    | 4    | u32   | aux_length        | 0                                                  |
| 116    | 8    | u64   | created_at        | Unix seconds — build timestamp                     |
| 124    | 132  | bytes | reserved          | Zeroed for future use                              |

## Spatial Index (Node → H3 cell)

H3 resolution 7 cells, sorted by `h3_cell` ascending. Each entry maps a
cell to a contiguous range of node IDs that fall within that cell.

**Index header**: `u32 entry_count` then `entry_count × IndexEntry`.

### Index Entry (24 bytes)

| Field      | Size | Type  | Description                            |
| ---------- | ---- | ----- | -------------------------------------- |
| h3_cell    | 8    | u64   | H3 resolution 7 cell index             |
| first_node | 4    | u32   | First node ID in this cell (inclusive) |
| node_count | 4    | u32   | Number of nodes in this cell           |
| edge_count | 4    | u32   | Total outgoing edges from cell's nodes |
| reserved   | 4    | bytes | Zeroed                                 |

## Node Table

Flat array of `node_count` fixed-size records. Each node represents a unique
intersection or endpoint in the road graph after union-find merging.

Node IDs are 0-indexed sequential (`0 .. node_count - 1`).

### Node Record (24 bytes)

| Offset | Size | Type | Field           | Description                                     |
| ------ | ---- | ---- | --------------- | ----------------------------------------------- |
| 0      | 4    | i32  | lat_micro       | Latitude in microdegrees (degrees × 100,000)    |
| 4      | 4    | i32  | lon_micro       | Longitude in microdegrees                       |
| 8      | 8    | u64  | osm_id          | OSM node/way ID (0 for merged/cross-road nodes) |
| 16     | 2    | u16  | road_class_bits | Bitmask: bit N = 1 if node is on road class N   |
| 18     | 2    | u16  | flags           | Reserved in v1                                  |
| 20     | 4    | u32  | \_reserved      | Zeroed                                          |

In v1, `osm_id`, `road_class_bits`, and `flags` are all zeroed (not yet populated).

## Edge Start Index

Flat `u32` array of `node_count + 1` entries. Entry `i` gives the start
offset into the edge table for node `i`. The sentinel at index `node_count`
equals the total edge count.

```
edges_for_node[i] = edges[edge_start[i] .. edge_start[i+1]]
```

Total size: `(node_count + 1) × 4 bytes`.

## Edge Table

Flat array of `edge_count` fixed-size records. Edges are sorted by source
node (matching edge start index order), then arbitrary.

### Edge Record (8 bytes)

| Offset | Size | Type | Field       | Description                              |
| ------ | ---- | ---- | ----------- | ---------------------------------------- |
| 0      | 4    | u32  | target_node | Target node ID (0-indexed)               |
| 4      | 2    | u16  | weight_cs   | Driving cost in centiseconds (1/100 sec) |
| 6      | 2    | u16  | flags       | Reserved in v1                           |

`weight_cs` is the traversal time for the **driving** profile, derived from
road segment length ÷ speed (with SPEED_FACTOR=0.85 multiplier). Weights
over 65535 cs (~11 min) are clamped to u16::MAX.

## Query Pattern (Not Yet Implemented)

1. **Open file**, read 256-byte header.
2. **Load spatial index** into memory for fast cell→node lookup.
3. **On route query:**
   a. Snap origin/dest to nearest nodes via spatial index (find H3 cell,
   binary-search nodes within cell, Haversine-NN).
   b. Run A*: for a node `n`, edges are `edges[edge_start[n]..edge_start[n+1]]`.
   Weight = edge.weight_cs (driving) or `edge.weight_cs * multiplier` (other profiles).
   c. Reconstruct path: sequence of node IDs → lookup coordinates from node table.

The advantage over the current per-query graph build:

- No 2s intersection-detection + union-find pass
- No Vec allocation for adjacency lists
- The flat arrays can be mmap'd and accessed with pointer arithmetic

## Estimated File Size (Tennessee)

| Component      | Size       | Notes                      |
| -------------- | ---------- | -------------------------- |
| Node table     | ~7 MB      | 300K nodes × 24 bytes      |
| Edge start idx | ~1.2 MB    | (300K + 1) × 4 bytes       |
| Edge table     | ~5 MB      | 600K edges × 8 bytes       |
| Spatial index  | ~0.5 MB    | 23K cells × 24 bytes + hdr |
| **Total**      | **~14 MB** | Uncompressed (mmap-able)   |

Roughly 2x the compressed estimate from the original design (no zstd dict),
but zero decompression cost.

## Build Pipeline

```
ptiles build-routing data/states/TN.roads.ptiles data/states/
```

1. Reads all road segments + intersections from `TN.roads.ptiles`
2. Calls `router::build_graph(segments, cell, "driving", intersections)`
3. Serializes the output `(node_count, adjacency, micro_coords, geo_coords)`
   to flat arrays
4. Builds H3 spatial index grouping nodes by res 7 cell
5. Writes header, index, node table, edge start index, edge table

## Multi-State Routing (Design)

Each state has its own `.routing.ptiles` file. The router loads files for
states the route passes through, merges edge tables (shifting node IDs),
and runs A\* on the combined graph.

Cross-state road segments exist in both states' `.roads.ptiles`. The builder
creates a node at the state line that appears in both routing files. At load
time, these duplicates are detected by proximity and merged.

**Not yet implemented.** The current CLI (`ptiles route <dir>`) loads
all `.roads.ptiles` files and builds the graph per-query. The routing ptiles
reader would replace this with flat-array load + A\*.

## What's Missing

| Feature                                 | Status              |
| --------------------------------------- | ------------------- |
| Builder                                 | Done                |
| Reader                                  | Not started         |
| Profile speed multipliers (aux section) | Not started         |
| Node metadata (osm_id, road_class)      | Zeroed placeholders |
| Highway routing variant                 | Not started         |
| Cross-state node dedup                  | Not started         |
| Turn penalties                          | Not started         |
| Component IDs                           | Not started         |
