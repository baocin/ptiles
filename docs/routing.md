# PTILES Routing Index (.routing.ptiles)

Offline point-to-point routing companion format for PTILES road tiles. Each
`.routing.ptiles` file sits alongside a `.roads.ptiles` file and contains
precomputed intra-cell portal distance matrices for H3-tiled routing.

| File | Magic | Layer | Geometry | Est. Size (US) |
|------|-------|-------|----------|----------------|
| `TN.routing.ptiles` | `PTILESU\0` | Routing | Portal graphs (no geometry) | ~50 MB (TN) / ~2 GB (US) |

---

## Algorithm: H3 Portal Stitching

The routing algorithm is inspired by Duan et al. 2025, *"Breaking the Sorting
Barrier for Directed Single-Source Shortest Paths"* (arXiv:2504.17033).

**Core insight:** H3 spatial tiling on road networks is equivalent to recursive
vertex partitioning by distance. Each H3 cell at resolution 7 (~5 km^2) contains
a subgraph of the road network. By precomputing distances between boundary
portals within each cell, routing becomes a cross-cell frontier expansion
between portal matrices instead of Dijkstra on the full graph.

```
Route query (A → B):
1. Convert A, B to H3 res-7 cells
2. If same cell: Dijkstra on local graph → done
3. Find LCA at coarser res to bound search
4. Expand frontier outward from origin cell (k-ring)
5. At each frontier cell: load portal distance matrix,
   compute shortest path from origin → each exit portal
6. When frontier reaches destination cell: stitch portal
   chain → concatenate full path
7. If beyond LCA bounds: climb to parent res and repeat
```

---

## File Format

Reuses the same shared PTILES header, spatial index, and zstd-compressed
per-cell blocks as all other PTILES layers (see `SPEC.md` for shared format
details).

### Header Magic

| Offset | Size | Value | Description |
|--------|------|-------|-------------|
| 0 | 8 | `PTILESU\0` | Magic, layer U = Urban navigation (routing) |
| 8 | 1 | `1` | Version |
| 9-11 | 3 | — | Reserved (zeroed) |
| 12 | 4 | float32 | Bounding box (copied from source roads file) |
| 16-84 | — | — | Standard PTILES header fields |
| 84 | 172 | — | Reserved |

### Per-Cell Block (zstd compressed)

Each H3 cell block contains a local road graph, portal definitions, and
precomputed distance matrix between portals.

```
node_count:        varint — total graph nodes in this cell
portal_count:      varint — number of portal nodes (0 if interior cell)
portals:           [node_id: varint] * portal_count
portal_links:      [neighbor_cell: uint64, neighbor_portal_idx: varint]
                   * portal_count — which adjacent cell + portal index
                   each portal connects to
distance_matrix:   flattened upper-triangular matrix
                   (portal_count * (portal_count-1) / 2 entries)
                   each entry: varint weight in centiseconds (1/100 s)
cell_bounds:       [neighbor_cell: uint64, min_weight: varint,
                    max_weight: varint] * neighbor_count
                   — min/max portal-pair cost to each adjacent cell
adjacency:         full local road graph for intra-cell Dijkstra
  node_edges:      [edge_count: varint,
                    edges: [target: varint, weight: varint]]
                   * node_count
```

**Coordinate encoding (for adjacency graph):** Road segment coordinates are
stored as i32 microdegrees (×100,000) with zigzag varint deltas between
consecutive vertices — same as the `.roads.ptiles` format.

**Weight encoding:** All edge weights are `u64` centiseconds (1/100 second).
This provides 0.01s precision for short segments while supporting routes up
to ~10,000 seconds (~2.8 hours) without overflow. Conversion:

```
weight_from_seconds(seconds: f64) -> u64  // seconds × 100, rounded
weight_to_seconds(weight: u64) -> f64     // weight / 100.0
```

**Varint encoding:** Standard unsigned LEB128 (same as PTILES codec).

---

## Build Pipeline

### Routing Index Builder

The companion file is built from an existing `.roads.ptiles` file by the
`routing-index-builder` CLI:

```
routing-index-builder <roads.ptiles> <output.routing.ptiles>
```

**Per-cell processing:**

1. **Read road segments** from the roads block for this H3 cell
2. **Build local directed graph** — each segment vertex becomes a node;
   each segment edge (vertex[i] → vertex[i+1]) becomes one or two directed
   graph edges depending on oneway tag. Edge weight computed as
   (segment length) / (speed), where speed comes from maxspeed tag or
   road-class default.
3. **Detect portal nodes** — nodes within 100m of the H3 cell boundary.
   These are typically road split points where the OSM way crosses the
   hexagon edge.
4. **Map portals to neighbors** — each portal is assigned to the adjacent
   H3 cell (among the 6 neighbors) whose center is closest to the portal
   coordinate.
5. **Compute intra-cell all-pairs shortest paths** — run Dijkstra from
   each portal source node to all other portal nodes. Store as compressed
   upper-triangular matrix.
6. **Compute cell-to-cell bounds** — for each pair of portals that share
   the same neighbor cell, record min/max distance between them.

**Edge weight formula:**
```
lat_scale = cos(cell_center_lat_radians)
dx = (lon2 - lon1) * lat_scale * 111,320
dy = (lat2 - lat1) * 111,320
meters = sqrt(dx² + dy²)
weight_cs = round(meters / (speed_mps) * 100)
```

Uses planar (flat-earth) approximation with latitude scale factor — replaces
Haversine for ~30× speedup with negligible accuracy loss at H3 res 7 scale.

### Speed Defaults by Road Class

| Road Class | Default (km/h) |
|------------|----------------|
| motorway, motorway_link | 105 |
| trunk, trunk_link | 90 |
| primary, primary_link | 65 |
| secondary | 55 |
| tertiary, tertiary_link | 45 |
| residential | 35 |
| service | 25 |
| track | 15 |
| footway, pedestrian | 5 |
| cycleway | 15 |
| path | 8 |
| other | 40 |

OSM `maxspeed` tag overrides the default when present.

### Oneway Handling

| `oneway` tag | Forward edge | Reverse edge |
|-------------|-------------|-------------|
| `"forward"` or `"yes"` | Added | Skipped |
| `"reverse"` | Skipped | Added |
| `"no"` or absent | Added | Added |

---

## Query Engine (PtilesRouter)

A Rust crate that reads `.routing.ptiles` + `.roads.ptiles` and executes
route queries.

```
PtilesRouter {
    routing_index: RoutingIndexReader,  // .routing.ptiles
    road_reader: RoadsReader,           // .roads.ptiles (for full geometry)
    cell_cache: LruCache<u64, CellData>,// LRU of recently used cells
}
```

### Route Algorithm (Frontier Expansion)

```
1. from_cell = h3::latlng_to_cell(from, Resolution::Seven)
   to_cell   = h3::latlng_to_cell(to,   Resolution::Seven)

2. If from_cell == to_cell:
      Run Dijkstra within the local cell graph.
      Return shortest path.

3. Find LCA at coarser resolution (climb res 7→6→5):
      The LCA defines the search boundary.

4. Expand frontier outward from from_cell:
      For each cell in the k-ring around from_cell:
        Load the cell's portal distance matrix.
        Compute shortest path from origin → each exit portal.
      (Frontier expansion = pulling from Duan et al.
       block-based linked list.)

5. When frontier reaches to_cell:
      Stitch: from → portal chain across cells → to.
      Return concatenated path with full geometry from roads layer.

6. If frontier exceeds LCA bounds without reaching to_cell:
      Climb to parent resolution and repeat.
```

### Constraint Profiles

Query-time filtering by road class:

| Profile | Road classes included |
|---------|----------------------|
| `driving` | All motorized: motorway through service |
| `walking` | footway, pedestrian, path, residential, service |
| `cycling` | cycleway, path, residential, service, tertiary |
| `no-highways` | All except motorway, trunk |
| `avoid-toll` | All (toll filtering TBD) |

---

## Codebase

### Files

| Path | Purpose |
|------|---------|
| `~/kino/projects/timeline/ptiles/src/routing.rs` | Routing format spec, reader, builder |
| `~/kino/projects/timeline/ptiles/src/bin/routing_index_builder.rs` | CLI binary |
| `~/kino/projects/timeline/ptiles/src/roads.rs` | Road segment reader (input to builder) |
| `~/kino/projects/timeline/docs/ptiles/offline-routing-prd.md` | Original PRD |

### Rust API

```rust
// Build routing index
let builder = RoutingIndexBuilder::new("TN.roads.ptiles", "TN.routing.ptiles");
builder.build()?;

// Load and query (future PtilesRouter)
let reader = RoutingReader::open("TN.routing.ptiles")?;
let data = reader.get_cell_data(cell_index)?;
// Returns portal list, distance matrix, adjacency graph
```

### JavaScript API (planned)

```js
import { PtilesRouter } from 'ptiles-router';

// Local mode: load full file
const router = await PtilesRouter.load('TN.routing.ptiles');

// Remote mode: fetch cells on demand from HTTP endpoint
const router = new PtilesRouter('https://maps.example.com/');

// Route query
const route = await router.route(
  { lat: 36.16, lng: -86.78 },  // Nashville
  { lat: 35.05, lng: -85.31 },  // Chattanooga
  { profile: 'driving' }
);
```

---

## Benchmarks

### TN Build (31 MB roads file, 23,087 cells, 1.2M segments)

| Configuration | Time | Speedup |
|---|---|---|
| Debug, unoptimized | 56.3s | 1x |
| Debug + all 4 optimizations | 7.0s | 8x |
| Release + all 4 optimizations | **1.87s** | **30.1x** |

### Output File Sizes

| Region | Roads Size | Routing Size | Ratio |
|--------|-----------|-------------|-------|
| Tennessee | 31 MB | 48 MB | 1.55x |
| US (est.) | ~1 GB | ~2 GB | ~2x |

### Optimizations Applied

1. **Planar distance** — Replaced Haversine (sin/cos/atan2) with flat-earth
   approximation using latitude scale factor computed once per cell. Small
   gain, mainly reduces per-edge trig overhead.

2. **Rayon parallelism** — Cells are fully independent. Pre-load compressed
   blocks, process with `par_iter()`. ~8x speedup on 12 cores.

3. **Sequential block scan** — Read all compressed blocks upfront instead
   of seek + read per cell. Eliminates 23K syscalls.

4. **Progress logging** — Every 500 cells instead of every cell.

---

## References

- Duan et al. 2025. *"Breaking the Sorting Barrier for Directed Single-Source
  Shortest Paths"*. arXiv:2504.17033. https://arxiv.org/abs/2504.17033
- PTILES Multi-Layer Specification: `SPEC.md`
- PTILES Ingest Pipeline: `PIPELINE.md` (in timeline monorepo)
- Offline Routing PRD: `~/kino/projects/timeline/docs/ptiles/offline-routing-prd.md`
- Rust reader implementation: `~/kino/projects/timeline/ptiles/src/routing.rs`
- DapStack ticket: ptil-16 (feature), ptil-17 (index builder), ptil-18 (query engine)
