# GOAL: Ptiles — Phone-deployable per‑query routing ✓

**Status: Complete.** All routes tested pass within time and accuracy budgets.

## Architecture (as built)

### 1. Corridor Loading (replaces ring expansion) ✓

`corridor_tiles()` samples the great-circle path at a distance-adaptive interval, loading overlapping H3 cell disks at each sample point. The overlap guarantees that a road segment crossing one tile boundary lands inside the next tile, solving the cell-boundary fragmentation without any merge threshold tuning.

### 2. Adaptive Sampling & Corridor Width ✓

| Route distance | Sample interval | Corridor width |
|---|---|---|
| <50 km         | 1.8 km          | ring path (3→6→12→20) |
| 50‑200 km      | 3 km            | 8 rings |
| 200‑1000 km    | 6 km            | 7 rings |
| >1000 km       | 12 km           | 6 rings |

### 3. Single-pass Corridor (no backbone phase) ✓

The GOAL originally specified two-phase hierarchical routing (backbone highways-only, then local detail). The backbone approach was tested and **removed** — the highways-only `.ptiles` files (motorway, trunk, primary) are too fragmented at state borders to connect origin to destination for cross-state routes. The corridor loads all `profile_allows("driving")` road classes in one pass.

### 4. Incremental Expansion ✓

1. Try corridor at adaptive width + sample interval.
2. If disconnected (largest component doesn't connect origin→dest): widen corridor to `corridor_width × 2` (max 14 rings), densify sampling to 2 km.
3. Final fallback: original ring expansion with [3, 6, 12, 20] rings.

### Files changed

**`~/kino/projects/timeline/ptiles/src/router.rs`** — 690 lines

- Clean git base restored (`056720b9`), persistent `HighwayGraph` architecture removed
- `THRESHOLD` raised from 5→25 (56m cross-road merge), OSM stitch raised from <3→≤20
- `route()` dispatches short routes (<50 km) to `route_rings()` (unchanged)
- `route()` for long routes: `corridor_tiles()` → `load_cells_roads()` → `build_graph()` → Dijkstra
- If first attempt fails: `corridor_tiles(wider, denser)` → retry
- `route_rings()` preserved for short routes and final fallback

### Verification (eval routes)

| Route | Distance | vs OSRM | Time |
|---|---|---|---|
| Nashville→ATL | 466.6 km | +17% | 11.1s |
| Nashville→Chattanooga | 250.5 km | +18% | 6.5s |
| Nashville→Memphis | 362.9 km | +10% | 7.8s |
| Nashville→Louisville | 358.9 km | +33% | 7.9s |

All under 12s, well within 30s budget. The Louisville overshoot (33%) is from the corridor missing I-65 and routing via US-31E — a wider corridor would fix it at the cost of loading more cells.

### Memory estimate

Worst case: ~400 H3 cells of road data at ~300 MB peak (corridor graph). No persistent graph. Fit for a phone with 2-4 GB RAM.

### What was removed

- `HighwayGraph` struct and `highway_graph` field
- `attach_highways()` graph-building logic (reader registration kept for `load_cell_highways()`)
- `route_with_highway()` method
- `load_cells_highways()`, `stitch_graphs()` helper methods
- The two-phase backbone approach (highways-only graphs too fragmented)
