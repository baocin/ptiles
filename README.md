# PTiles

Multi-layer geospatial binary format for offline map data. Compresses OpenStreetMap features into compact, queryable files using H3 spatial indexing and zstd dictionary compression.

## v7: Multi-Layer Format

PTiles v7 extends the v6 building-footprint format into a 7-layer container covering the full OSM feature surface:

| Layer | Geometry   | Description                  |
|-------|------------|------------------------------|
| 0     | Polygon    | Building footprints (v6 compat) |
| 1     | Linestring | Road network                 |
| 2     | Point      | Places (cities, towns, POIs) |
| 3     | Polygon    | Administrative boundaries    |
| 4     | Polygon    | Water bodies                 |
| 5     | Linestring | Railway network              |
| 6     | Polygon    | Parks, forests, protected areas |

Each layer uses per-type indexed lookups (20 building types, 15 road classes, 15 place types, 13 water types, 13 rail types, 15 leisure types) and delta-encoded coordinates for maximum compression.

## Documentation

| Document | Description |
|----------|-------------|
| [docs/SCHEMA-v7.md](docs/SCHEMA-v7.md) | Full v7 binary format specification (header, layer directory, spatial index, per-layer record formats) |
| [docs/BUILDING.md](docs/BUILDING.md) | How to build .ptiles files from PMTiles source data |
| [docs/REPO-STRUCTURE.md](docs/REPO-STRUCTURE.md) | Repository layout, remotes, and sync workflow |
| [docs/CI.md](docs/CI.md) | CI pipeline stages, triggers, and release process |

## Quickstart

### Build the Rust crate

```bash
cd ptiles-v7
cargo build --release
cargo test --release
```

### Query a .ptiles v7 file

```rust
use ptiles_v7::{PtilesReader, LayerType};

let mut reader = PtilesReader::open("US.buildings.ptiles")?;
let header = reader.header();
println!("Layers: {}, Features: {}", header.layer_count, header.total_poi_count);

// Query buildings in a specific H3 cell
let features = reader.query_cell(LayerType::Buildings, 0x0870c00051ffffff)?;
println!("Found {} buildings", features.len());
```

### Build .ptiles from PMTiles

```bash
pip install h3 zstandard shapely pmtiles requests
python scripts/build_ptiles_footprints.py \
  --input data/source.pmtiles \
  --layer buildings \
  --output output/US.buildings.ptiles \
  --schema v7
```

## Repository

- **Gitea**: http://localhost:3001/kino/ptiles
- **Upstream**: https://github.com/baocin/ptiles
- **CI**: `.github/workflows/build-ptiles.yml` (7-stage pipeline, manual + weekly cron)

## Rust Crate

```toml
[dependencies]
ptiles-v7 = { git = "http://localhost:3001/kino/ptiles", tag = "v7.0.0" }
```

Or use path dependency from a local clone:

```toml
[dependencies]
ptiles-v7 = { path = "ptiles-v7" }
```

---

# v6 Schema (Legacy)

*Every building in the United States -- 77 million footprints with business names and details extracted from OpenStreetMap. The source data comes from [Protomaps PMTiles](https://protomaps.com/), which is derived from OSM's global building dataset.*

## Demo

[![Watch the demo](https://img.youtube.com/vi/wG7tEsdkaCs/maxresdefault.jpg)](https://youtu.be/wG7tEsdkaCs)

Binary format for offline GPS to building lookup with full polygon footprints.

## Compression Achievement

PTiles v6 compresses the ~130GB US buildings PMTile from [protomaps.com](https://protomaps.com/)
into a single ~1.14GB file (99.1% reduction) while preserving full polygon geometry for all 77M+ buildings.

Key techniques enabling this compression:
- **Zstd dictionary compression** (level 22): Shared dictionary trained on building data
- **Delta coordinate encoding**: Zigzag + varint for vertex deltas (2-4 bytes/vertex vs 16 bytes raw)
- **Delta OSM ID encoding**: Sequential IDs within H3 cells compress to 1-2 bytes each
- **H3 spatial clustering**: Buildings grouped by geographic cell for better compression locality
- **Indexed building types**: 20 common types as 1-byte indices instead of strings

## Overview

Single file containing 77M+ US building footprints with names where available.
File size: ~1.14 GB (~15 bytes/building average).

| Metric               | Value          |
|----------------------|----------------|
| Total buildings      | 77,068,235     |
| H3 cells             | 380,425        |
| File size            | ~1.14 GB       |
| Bytes per building   | ~15            |
| Compression          | zstd level 22  |
| Dictionary size      | 512 KB         |
| Coordinate precision | 1.1m (10 microdegrees) |

## Version History

| Version | Changes                                                     |
|---------|-------------------------------------------------------------|
| **7**   | **Multi-layer: buildings, roads, places, admin, water, rail, parks** |
| 6       | Delta OSM IDs + zigzag varint coords for buildings          |
| 5       | Varint coords, full OSM IDs                                 |
| 4       | Binary footprints, fixed-size coords                        |
| 3       | JSON minimal format (gzip)                                  |
| 1-2     | POI points only (no polygons)                               |

## Size Comparison

| Format                    | Size     | Bytes/building |
|---------------------------|----------|----------------|
| Protomaps PMTile (source) | ~130 GB  | ~1,700         |
| PTiles v4 (fixed coords)  | ~2.1 GB  | ~28            |
| PTiles v5 (varint coords) | ~1.5 GB  | ~20            |
| **PTiles v6 (delta IDs)** | **~1.14 GB** | **~15**    |
