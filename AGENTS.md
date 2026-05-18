# ptiles — Agent Instructions

Ptiles-specific information only. Generic project management (DapStack, Hermes
profiles, user preferences) is handled by the agent's memory and cron system,
not this file.

## Two-Repo Architecture

The PTILES project spans two repos. Know which one you're in.

| Repo | Path | What |
|------|------|------|
| **Upstream (this one)** | `~/kino/projects/ptiles/` | Python build scripts, format spec, build data, docs |
| **Downstream (timeline)** | `~/kino/projects/timeline/ptiles/` | Rust reader crate, CLIs, routing engine |

The upstream builds `.ptiles` files using Python on Linux. The downstream
consumes them via Rust. The upstream `SPEC.md` is the canonical format spec;
the downstream crate mirrors it in code.

## Data Sources

Six data source families feed the PTILES build pipeline. Every layer traces
back to one of these.

### 1. Geofabrik OSM State PBFs

All OSM-derived layers (roads, water, buildings, places, rail, parks) start
from state-level OSM extracts.

**Location:** `~/kino/projects/ptiles/data/pbfs/` — 51 files, ~11 GB
**Source URL:** https://download.geofabrik.de/north-america/us/
**Format:** `.osm.pbf` (Protocolbuffer Binary Format, zlib-compressed)
**Tool:** `osmium` Python bindings (`import osmium`, requires `locations=True`)
**Update frequency:** Daily (Geofabrik rebuilds every 24h)
**License:** ODbL (Open Database License) — attribution required

State name convention: lowercase-hyphenated
```
tennessee-latest.osm.pbf   north-carolina-latest.osm.pbf   new-york-latest.osm.pbf
```

```bash
# Download single state
wget https://download.geofabrik.de/north-america/us/tennessee-latest.osm.pbf

# Download all states
wget -i <(curl -sL https://download.geofabrik.de/north-america/us/ | \
    grep -oP 'href="[^"]+-latest\.osm\.pbf"' | tr -d 'href="')
```

**Used by:**
- `build_state_v8.py` — buildings
- `build_roads.py` — roads
- `build_water.py` — water features
- `build_tn_v8.py` — TN-only buildings test

### 2. Overture Maps (Buildings + Places)

Two separate datasets from the Overture Maps Foundation.

#### Building Footprints
**Format:** PMTiles (single-file tile archive, zstd-compressed MVT)
**Old path:** `~/data/protomaps/20260513.pmtiles` (23 GB) — BROKEN
  (go-pmtiles crashes SIGSEGV, Python pmtiles reader fails on varint stream)
**Not currently usable via any tool.** Per-state OSM PBFs are the working
alternative for building extraction.

**License:** Community Dataset Agreement (CDA) — free with attribution

#### Places / POIs
**Path:** `~/overture-2026-04-15.0/places/`
**Format:** 16 Zstandard-compressed Parquet files, 9.7 GB total
**Schema:** id, geometry (WKB), name, categories, addresses, phone, website,
  brand, social, email
**Used by:** `build_business.py` / `build_us_business.py`
**Update frequency:** Quarterly. Re-download URL pattern:
  `https://data.source.coop/overture-maps/release/{YYYY-MM-DD}/theme=places/type=place/`
**License:** Community Dataset Agreement (CDA)

```bash
# List Overture Places files
ls ~/overture-2026-04-15.0/places/
# Each file: part-XXXXX-{uuid}-c000.zstd.parquet (~595-624 MB each)
```

### 3. US Census Bureau TIGER/Line Shapefiles

Admin layer (states, counties, ZCTAs, timezones). All at 1:500k resolution.

| Layer | File | URL | Size |
|-------|------|-----|------|
| States | `cb_2023_us_state_500k.zip` | https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip | ~8 MB |
| Counties | `cb_2023_us_county_500k.zip` | https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip | ~18 MB |
| ZCTAs | `cb_2020_us_zcta520_500k.zip` | https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip | ~60 MB |
| Timezones | `combined.json` | https://github.com/evansiroky/timezone-boundary-builder/releases/latest/download/timezones.geojson.zip | ~25 MB |

**Cache (NFS):** `/mnt/core/timeline-ptiles-cache/admin_data/`
  (zip files readable; extracted shapefiles unreadable — owned by uid 501).
  Extract locally:
```bash
mkdir -p ~/admin-data/{states,counties,zcta,tz}
unzip -o /mnt/core/timeline-ptiles-cache/admin_data/cb_2023_us_state_500k.zip -d states/
unzip -o /mnt/core/timeline-ptiles-cache/admin_data/cb_2023_us_county_500k.zip -d counties/
unzip -o /mnt/core/timeline-ptiles-cache/admin_data/cb_2020_us_zcta520_500k.zip -d zcta/
cp /mnt/core/timeline-ptiles-cache/admin_data/tz/combined.json tz/
```

**License:** Public domain
**Update frequency:** States/counties annually, ZCTAs decennially

### 4. USGS 3DEP Elevation (future)

1/3-arc-second DEM (~10m resolution, seamless US coverage).
Not yet integrated — planned for routing v2 (elevation penalties on walking/cycling routes).

**Download:** https://www.usgs.gov/3dep
**Format:** GeoTIFF, distributed in 1°×1° tiles via AWS S3
**Size:** ~400 GB for CONUS

### 5. NRCS SSURGO (PrePerc / future)

Soil Survey Geographic Database for perc-rate estimation.

**Path:** Not yet downloaded. State-level File Geodatabases (~50-200 MB/state).
**Source:** https://websoilsurvey.nrcs.usda.gov/
**Critical table:** `cointerp` with rule `"Septic Tank Absorption Field"`
**License:** Public domain

(PrePerc is a separate downstream project; SSURGO data work there, not here.)

### 6. FEMA NFHL (future)

National Flood Hazard Layer for flood-zone queries.
Not yet integrated. Available as ArcGIS REST service and per-state geodatabases.

**REST endpoint:** `https://hazards.fema.gov/gis/NFHL/rest/services/NFHL/MapServer/0/query`
**License:** Public domain

## Data Source Quick Reference

| Source | On Disk Now? | Path | Size | Freshness |
|--------|-------------|------|------|-----------|
| OSM PBFs (51 states) | Yes | `data/pbfs/*.osm.pbf` | 11 GB | May 17 2026 |
| OSM PBFs (NAS cache) | Yes (if NFS mounted) | `/mnt/core/timeline-ptiles-cache/raw/*.osm.pbf` | 11 GB | Jan 16 2026 |
| Overture Places | Yes | `~/overture-2026-04-15.0/places/` | 9.7 GB | Apr 15 2026 |
| Overture Buildings PMTiles | Yes | `~/data/protomaps/20260513.pmtiles` | 23 GB | BROKEN |
| Census Shapefiles | Yes (NFS) | `/mnt/core/timeline-ptiles-cache/admin_data/` | 364 MB | 2023/2020 |
| Census Shapefiles (local) | No | `~/admin-data/` | — | Needs extract |
| SSURGO | No | — | — | Future |
| FEMA NFHL | No | — | — | Future |

## Build Commands

All Python scripts must use `uv run` — the system python is externally managed
and lacks geospatial packages. Always use `uv run --with <pkgs> python script.py`.

### Buildings
```bash
# Single state (per-state PBF)
uv run --with osmium --with h3 --with zstandard --with numpy --with shapely \
    python scripts/build_state_v8.py TN

# All 51 states
uv run --with osmium --with h3 --with zstandard --with numpy --with shapely \
    python scripts/build_state_v8.py --all
```

### Roads
```bash
uv run --with osmium --with h3 --with zstandard --with shapely \
    python scripts/build_roads.py \
    data/pbfs/tennessee-latest.osm.pbf \
    data/states/TN.roads.ptiles
```

### Water
```bash
uv run --with osmium --with h3 --with zstandard --with shapely \
    python scripts/build_water.py \
    --source pbf \
    --pbf data/pbfs/tennessee-latest.osm.pbf \
    --region tennessee \
    --output data/states/TN.water.ptiles
```

### Business / POIs
```bash
# TN only (old, hardcoded)
uv run --with pyarrow --with shapely --with h3 --with zstandard --with numpy \
    python scripts/build_business.py

# US-wide (single pass, ~30-60 min)
uv run --with pyarrow --with shapely --with h3 --with zstandard --with numpy \
    python scripts/build_us_business.py
```

### Admin (full US only, needs Census shapefiles)
```bash
uv run --with geopandas --with h3 --with numpy --with zstandard --with shapely \
    python scripts/build_admin.py /path/to/admin-data/ output/admin.ptiles
```

### Full US Batch
```bash
bash scripts/run_us_build.sh
```

## Rust Reader & CLI (downstream repo)

The Rust ptiles crate lives in the timeline monorepo:

**Path:** `~/kino/projects/timeline/ptiles/`

Contains readers for ALL layers (buildings, roads, admin, water, places, parks,
rail, plus the new routing format). Each layer gets its own module:
`buildings.rs`, `roads.rs`, `admin.rs`, `water.rs`, `places.rs`, `parks.rs`,
`rail.rs`, `routing.rs`.

### Readers (stable)
- All layers parse `.ptiles` files, return typed structs
- Shared codec, header, and spatial index modules
- CLI at `src/main.rs` — query any layer by lat/lon

### Routing (new, building)
- **Builder (`routing.rs`):** Complete. Reads `.roads.ptiles`, detects portal
  nodes, computes APSP, writes `.routing.ptiles`. CLI binary
  `routing-index-builder`. 1.87s for TN (release).
- **Query engine (`PtilesRouter`):** Not yet built (ptil-18). Planned as a
  new module for frontier-expansion routing.

### Building
```bash
cd ~/kino/projects/timeline
cargo build -p ptiles           # debug
cargo build -p ptiles --release # release
```

### CLI Tools
```bash
# Query any ptiles file
cargo run -p ptiles --bin ptiles -- TN.roads.ptiles 36.16 -86.78

# Build routing index (companion to .roads.ptiles)
cargo run -p ptiles --bin routing-index-builder -- TN.roads.ptiles TN.routing.ptiles

# Debug portal detection
cargo run -p ptiles --bin routing-debug -- TN.roads.ptiles 872648000ffffff

# Scan for portal threshold calibration
cargo run -p ptiles --bin scan-portals -- TN.roads.ptiles
```

### Binary names
| Binary | Path | Purpose |
|--------|------|---------|
| `ptiles` | `src/main.rs` | General query CLI for all layers |
| `routing-index-builder` | `src/bin/routing_index_builder.rs` | Build .routing.ptiles from .roads.ptiles |
| `routing-debug` | `src/bin/routing_debug.rs` | Debug portal detection per cell |
| `scan-portals` | `src/bin/scan_portals.rs` | Calibrate portal distance threshold |

## JavaScript Client Library (ptil-19)

**Path:** `~/kino/projects/ptiles/js/ptiles-client.js`

Currently a skeleton — 39 lines, class stub with TODOs. No binary decoder,
no routing engine, no H3 lookup implemented yet. Planned features:

- Binary decoder for `.ptiles` and `.routing.ptiles` formats
- Both local (Node fs) and remote (HTTP fetch) modes
- PtilesRouter port from Rust (same frontier-expansion algorithm)
- TypeScript types, npm package

No dependencies installed (no `package.json` yet). Progress tracked in
DapStack ticket ptil-19.

## Format / Spec

- **Canonical spec:** `SPEC.md` (multi-layer format)
- **Routing format:** `docs/routing.md` (companion .routing.ptiles format)
- **Packing catalog:** `docs/packing-catalog.md` (4-tier enhancement ideas)
- **Build status:** `current_progress.md` (latest benchmarks and metrics)
- **Downstream pipeline docs:** `~/kino/projects/timeline/ptiles/PIPELINE.md`

All `.ptiles` files share a common header (256 bytes, PTILES + layer byte),
zstd-compressed per-cell blocks, and a spatial index sorted by H3 cell.

### Layer Magic Bytes
| Byte | ASCII | Layer |
|------|-------|-------|
| `0x46` | `F` | Buildings (footprints) |
| `0x52` | `R` | Roads |
| `0x41` | `A` | Admin boundaries |
| `0x57` | `W` | Water |
| `0x50` | `P` | Places |
| `0x4E` | `N` | Parks |
| `0x54` | `T` | Rail/transit |
| `0x49` | `I` | POIs |
| `0x44` | `D` | Address ranges |
| `0x55` | `U` | Routing (companion format) |

## h3-py v4 API Quirks

The `h3` Python library v4 renamed several functions. Scripts in this repo
target the v4 API:

| Old (v3) | New (v4) |
|----------|----------|
| `h3.geo_to_h3(lat, lon, res)` | `h3.latlng_to_cell(lat, lon, res)` |
| `h3.h3_to_geo(cell)` | `h3.cell_to_latlng(cell)` |

**Pitfall:** `h3.latlng_to_cell()` can return a hex string OR an integer
depending on version. Always normalize:
```python
cell_hex = h3.latlng_to_cell(lat, lon, res)
if isinstance(cell_hex, int):
    cell_hex = hex(cell_hex)[2:]
```

**Pitfall:** `h3.cell_to_latlng()` expects a hex string input, NOT an int.

## osmium `locations=True` Requirement

Every osmium handler that accesses node coordinates MUST use:
```python
handler.apply_file(pbf_path, locations=True)
```

Without this, any `w.nodes[0].lon` call raises
`osmium._osmium.InvalidLocationError`.

## NFS Mount

The NAS at `100.94.73.109:/mnt/tmp/core` mounts to `/mnt/core/`.

```bash
# Check if mounted
mount | grep /mnt/core

# Mount
sudo mount /mnt/core
```

Contains cached PBFs (Jan 16) and admin shapefiles. The local PBFs at
`data/pbfs/` (May 17) are fresher and preferred.

## Zstd Dictionary Training

For the roads layer, train a zstd dictionary on the first ~10,000 blocks
before final compression. For other layers, the first ~500-1000 blocks.
Dictionary training reduces block size by ~30%.

## R2 Upload

Upload built `.ptiles` files to Cloudflare R2 for app consumption:
```bash
AWS_PROFILE=mdt-r2 aws s3 cp data/states/TN.buildings_v8.ptiles \
    s3://mydatatimeline/maps/TN.buildings_v8.ptiles
```

**Profile:** `mdt-r2` (configured in `~/.aws/config` and `~/.aws/credentials`)
**Bucket:** `mydatatimeline` (contains `downloads/`, `maps/`, `models/`)

## Free-Space Guide

Before any large download, build, or data processing, verify available disk
space. Rough sizes:
- OSM PBFs: 11 GB (51 files)
- Overture Places parquet: 9.7 GB (16 files)
- Per-state extracted/intermediate data: 2-5x source size during processing
- Final ptiles output: ~4 GB for full US (buildings + roads + water + admin + business)

## Commit Convention

Prefix commits with the ticket number: `[PTL-123] feat: ...`
Changes to the downstream Rust reader should use `[MDT-xxx]` prefix.
