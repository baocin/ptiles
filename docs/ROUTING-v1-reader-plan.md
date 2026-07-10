# Routing Algo Update — .routing.ptiles Reader

## Goal

Make the `ptiles route` subcommand (and `PtilesRouter`) read `.routing.ptiles`
flat-array files instead of building the graph from `.roads.ptiles` per query.

## Status Quo

- `ptiles route <roads_dir> <lat1> <lon1> <lat2> <lon2>`:
  1. Loads road segments from `.roads.ptiles` files for corridor cells
  2. Calls `build_graph()` → intersection detection + union-find merge (2s)
  3. Runs A\* on the built adjacency list

- `.routing.ptiles` files exist for TN (built via `build-routing` subcommand)
  but can only be read by the builder, not by the router.

## What to Build

### 1. Create `src/routing_reader.rs` — Flat-array graph reader

Struct `RoutingReader` that opens a `.routing.ptiles` file and exposes:

```rust
pub struct RoutingReader {
    // mmap'd file or buffer pointers
    node_table: &[u8],     // 24 bytes per node
    edge_start: &[u32],    // (node_count + 1) entries
    edge_table: &[u8],     // 8 bytes per edge
    node_index: &[u8],     // 24 bytes per entry + 4 byte header
    node_count: usize,
    edge_count: usize,
}

impl RoutingReader {
    pub fn open(path: impl AsRef<Path>) -> Result<Self>;
    pub fn node_count(&self) -> usize;
    pub fn edge_count(&self) -> usize;
    pub fn get_node_coords(&self, node_id: u32) -> (f64, f64); // lat, lon
    pub fn get_outgoing_edges(&self, node_id: u32) -> &[EdgeRecord];
    pub fn find_nearest_node(&self, lat: f64, lon: f64) -> Option<u32>;
    pub fn find_cell_nodes(&self, cell: u64) -> Option<(u32, u32)>; // first, count
}
```

`EdgeRecord` struct:

```rust
#[repr(C)]
pub struct EdgeRecord {
    pub target: u32,
    pub weight_cs: u16,  // centiseconds, clamped
    pub flags: u16,      // reserved
}
```

### 2. A\* on flat arrays

New `astar_flat()` that takes a `RoutingReader` ref instead of `Vec<Vec<(u32, Weight)>>`:

```rust
fn astar_flat(
    routing: &RoutingReader,
    src: u32,
    dst: u32,
    dst_lat: f64,
    dst_lon: f64,
    multiplier: f64,  // 1.0 for driving, ~0.3 for walking
) -> (Vec<Weight>, Vec<u32>)
```

Key differences from existing `astar_with_pred`:

- No adjacency Vec — reads `routing.get_outgoing_edges(node_id)` each time
- Weight multiplier: `actual_weight = (edge.weight_cs as f64 * 256.0 / multiplier_q8) as Weight`
- Same FIFO heap, same haversine heuristic

### 3. Integrate into `PtilesRouter`

Add a `routing_file: Option<PathBuf>` field. When set, skip `build_graph` and
read from the routing file instead:

```rust
// In PtilesRouter::route()
if let Some(ref routing_path) = self.routing_file {
    let routing = RoutingReader::open(routing_path)?;
    // Find nearest nodes, run astar_flat, reconstruct path
} else {
    // Current path (load cells, build_graph, astar_with_pred)
}
```

CLI: Add `--routing <file>` flag to `ptiles route`.

### 4. Multi-state merge

When the route crosses state lines, load routing files for all intersected states.
Merge by shifting node IDs: state B's nodes = (state A's node_count + state B's node_id).

Cross-state duplicate nodes (same road at border) need proximity-based dedup
at load time. Skip for v1 — single-file routing first.

### 5. Coordinate lookup

Node coords are in microdegrees (i32 × 100,000). `get_node_coords()` reads
from the node table at `node_id * 24` offset.

### 6. Nearest node via H3 spatial index

`find_nearest_node()`:

1. Compute H3 res 7 cell at (lat, lon)
2. Binary search the spatial index for that cell
3. Within matching cells, compute haversine distance to each node
4. Return closest

### 7. Profiles

v1 stores driving weights only (u16 centiseconds). Walking/cycling apply a
speed multiplier at query time. The routing file header stores 1 for
`profile_count` — no aux section yet. Multiplier is hardcoded in Rust:

```rust
fn profile_multiplier(profile: &str) -> f64 {
    match profile {
        "driving" => 1.0,
        "walking" => 0.3,
        "cycling" => 0.5,
        _ => 1.0,
    }
}
```

### 8. Path reconstruction

Same as existing `reconstruct_path()` — follow predecessor array from dst to src,
look up lat/lon from node table.

## Not in v1

- Aux section: profile speed tables, road class names, OSM way metadata
- Turn penalties
- Component IDs for faster endpoint snapping
- Cross-state node dedup
