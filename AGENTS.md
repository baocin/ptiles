# ptiles — Agent Instructions

Ptiles-specific information only. Generic project management (DapStack, Hermes
profiles, user preferences) is handled by the agent's memory and cron system,
not this file.

## Two-Repo Architecture

The PTILES project spans two repos. Know which one you're in.

| Repo                      | Path                               | What                                    |
| ------------------------- | ---------------------------------- | --------------------------------------- |
| **Upstream (this one)**   | `~/kino/projects/ptiles/`          | Python build scripts, format spec, docs |
| **Downstream (timeline)** | `~/kino/projects/timeline/ptiles/` | Rust reader crate, CLIs, routing engine |

## Data Location

All PTILES build data lives on NFS at `/mnt/core/kino/ptiles/data/`. The repo's
`data/` directory is a symlink to that path. If the symlink is missing, recreate it:

```bash
ln -s /mnt/core/kino/ptiles/data ~/kino/projects/ptiles/data
```

The upstream builds `.ptiles` files using Python on Linux. The downstream
consumes them via Rust. The upstream `SPEC.md` is the canonical format spec;
the downstream crate mirrors it in code.

## Data Sources

Six data source families feed the PTILES build pipeline. Every layer traces
back to one of these.

### 1. Geofabrik OSM PBFs

All OSM-derived layers (roads, water, buildings, places, rail, parks) start
from Geofabrik extracts — per state in the US, per country or per region
elsewhere. See **Non-US Regions** below for how non-US extracts are named,
declared and built.

**Location:** `/mnt/core/timeline-ptiles-cache/raw/` — 53 US files ~11 GB, plus
the Japan country and 8 region extracts (~4.7 GB)
**Source URL:** https://download.geofabrik.de/north-america/us/ (US),
https://download.geofabrik.de/asia/japan/ (Japan regions)
**Format:** `.osm.pbf` (Protocolbuffer Binary Format, zlib-compressed)
**Tool:** `osmium` Python bindings (`import osmium`, requires `locations=True`)
**Update frequency:** Daily (Geofabrik rebuilds every 24h)
**License:** ODbL (Open Database License) — attribution required

Extract name convention: lowercase-hyphenated, matching Geofabrik

```
tennessee-latest.osm.pbf   north-carolina-latest.osm.pbf   new-york-latest.osm.pbf
japan-latest.osm.pbf       kanto-latest.osm.pbf            kyushu-latest.osm.pbf
```

Do not hardcode this mapping in a builder — `states.py:pbf_path()` resolves a
scope to its extract across every cache directory and both naming conventions.

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
- `build_us_highways.py` — highways
- `build_points.py` — cameras and signals

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

**Path:** `~/overture-places/` (empty — data was used by build scripts and no longer needed on disk; re-download from source.coop if rebuilding from scratch)
**Format:** 16 Zstandard-compressed Parquet files, 9.7 GB total
**Schema:** id, geometry (WKB), name, categories, addresses, phone, website,
brand, social, email
**Used by:** `build_full_ptilesb.py`
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

| Layer     | File                          | URL                                                                                                    | Size   |
| --------- | ----------------------------- | ------------------------------------------------------------------------------------------------------ | ------ |
| States    | `cb_2023_us_state_500k.zip`   | https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip                               | ~8 MB  |
| Counties  | `cb_2023_us_county_500k.zip`  | https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip                              | ~18 MB |
| ZCTAs     | `cb_2020_us_zcta520_500k.zip` | https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip                             | ~60 MB |
| Timezones | `combined.json`               | https://github.com/evansiroky/timezone-boundary-builder/releases/latest/download/timezones.geojson.zip | ~25 MB |

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

### 4. USGS 3DEP Elevation (complete)

1/3-arc-second DEM (~10m resolution, seamless US coverage).
**All 49 CONUS+DC states downloaded.**

- **Path:** `/mnt/core/data/elevation/usgs-3dep/{STATE}/`
- **Total:** ~4,800 tiles, 1,008 GB
- **Dead states:** NH (15 tiles), VT (28 tiles) — these states' unique API products are fewer than 100
- **Script:** `/mnt/core/app-decompilations/scrapers/download_national_3dep.py`
- **Download command:**

```bash
python3 /mnt/core/app-decompilations/scrapers/download_national_3dep.py --states GA,KY
```

- **Status:** Downloaded, not yet integrated into PTILES pipeline — planned for routing v2 (elevation penalties on walking/cycling routes)

**Download:** https://www.usgs.gov/3dep
**Format:** GeoTIFF, distributed in 1°×1° tiles via AWS S3
**Size:** ~400 GB for CONUS

### 5. NRCS SSURGO (PrePerc / future)

Soil Survey Geographic Database for perc-rate estimation.

**Path:** Not yet downloaded. State-level File Geodatabases (~50-200 MB/state).
**Source:** https://websoilsurvey.nrcs.usda.gov/
**Critical table:** `cointerp` with rule `"Septic Tank Absorption Field"`
**License:** Public domain

**Caveat on "perc-rate estimation":** a good chunk of states do not use perc
tests at all. Virginia plus at least NC, IL, MN, ME, WI and OR evaluate septic
sites by licensed soil morphology evaluation (a professional reading the soil
profile in a pit or boring), so no perc-rate records exist there to estimate
or validate against — only morphology/permit records. Verified regulatory
citations per state live in the preperc repo at
`.claude/commands/goal-govdata-states.md`.

(PrePerc is a separate downstream project; SSURGO data work there, not here.)

### 6. FEMA NFHL (future)

National Flood Hazard Layer for flood-zone queries.
Not yet integrated. Available as ArcGIS REST service and per-state geodatabases.

**REST endpoint:** `https://hazards.fema.gov/gis/NFHL/rest/services/NFHL/MapServer/0/query`
**License:** Public domain

## Data Source Quick Reference

| Source                     | On Disk Now?    | Path                                              | Size     | Freshness   |
| -------------------------- | --------------- | ------------------------------------------------- | -------- | ----------- |
| Parquet v2 (8 layers)      | Yes             | `/mnt/core/kino/ptiles/data/parquet/v2/{STATE}/`  | 68 GB    | Jun 2026    |
| Parquet v1                 | Yes             | `/mnt/core/kino/ptiles/data/parquet/*_v1.parquet` | 22 GB    | 2025-2026   |
| .ptiles built tiles        | Yes             | `/mnt/core/kino/ptiles/data/states/`              | 7 GB     | Jun 2026    |
| OSM PBFs (1 state)         | Partial         | `/mnt/core/kino/ptiles/data/pbfs/*.osm.pbf`       | 140 MB   | May 17 2026 |
| OSM PBFs (full, NFS)       | Yes             | `/mnt/core/timeline-ptiles-cache/raw/*.osm.pbf`   | 11 GB    | Jan 16 2026 |
| Overture Places            | No (was 9.7 GB) | `~/overture-places/` — re-download if needed      | --       | --          |
| Overture Buildings PMTiles | Yes/BROKEN      | `~/data/protomaps/20260513.pmtiles`               | 23 GB    | BROKEN      |
| Census Shapefiles          | Yes (NFS)       | `/mnt/core/timeline-ptiles-cache/admin_data/`     | 364 MB   | 2023/2020   |
| USGS 3DEP Elevation        | Yes             | `/mnt/core/data/elevation/usgs-3dep/`             | 1,008 GB | Jun 23 2026 |

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
# All 51 states -> {ST}.business_v4.ptiles (resumes; skips states already built)
uv run --with pyarrow --with shapely --with h3 --with zstandard --with numpy \
    python scripts/build_full_ptilesb.py

# Name-prefix search index, built from the business_v4 file above
uv run --with h3 --with zstandard \
    python scripts/build_business_name_index.py TN
```

`build_full_ptilesb.py` is the only builder that emits the shipped v4 format.
The older `build_business.py` (v1) and `build_us_poi*.py` (v3) were deleted;
see `scripts/README.md` for the full layer-to-builder map.

### Admin (full US only, needs Census shapefiles)

```bash
uv run --with geopandas --with h3 --with numpy --with zstandard --with shapely \
    python scripts/build_admin.py /path/to/admin-data/ output/admin.ptiles
```

### Full US Batch

```bash
bash scripts/run_us_build.sh
```

## Non-US Regions

The pipeline is not US-only. Japan is built and published; anything Geofabrik
ships an extract for can follow the same path. What differs from the US case is
mostly naming and granularity.

### Scopes

A *scope* is the prefix of a `.ptiles` filename — the area one file covers.

| Scope       | Means                          | Example file                   |
| ----------- | ------------------------------ | ------------------------------ |
| `TN`        | a US state                     | `TN.buildings_v9.ptiles`       |
| `US`        | the whole US                   | `US.admin.ptiles`              |
| `JP`        | a whole country (alpha-2)      | `JP.places_v1.ptiles`          |
| `CAN`       | a whole country (alpha-3)      | `CAN.places_v1.ptiles`         |
| `JP-KANTO`  | a subdivision of one           | `JP-KANTO.buildings_v9.ptiles` |

A bare two-letter scope is ambiguous by itself — `TN` is Tennessee *and*
Tunisia — so `ptiles/scopes.py` resolves it against the 51 US abbreviations,
which own the unprefixed namespace because they were published first.
`country_of("DE")` returns `US`: Delaware, not Germany.

**26 ISO country codes collide with a US state abbreviation**: AL AR AZ CA CO DE
GA ID IL IN KY LA MA MD ME MN MO MS MT NC NE PA SC SD TN VA. Such a country must
use its **ISO alpha-3 code** (`CAN`, `DEU`, `TUN`) or subdivision scopes
(`CA-ON`). Three letters can never be a state abbreviation, so alpha-3 is
always unambiguous.

This is enforced, not just documented: `states.py` refuses a `NON_US` row whose
scope resolves back to the US, at import. Without that guard Canada's
`CA.places_v1.ptiles` would publish to exactly California's key and overwrite it
in the bucket, with no error at any step.

`ptiles/scopes.py` is the single definition of this and of the published
layout; the manifest writer, the uploader and both clients all import it rather
than reimplementing the rule.

### Declaring a region

Regions live in `scripts/states.py`. Non-US entries go in `NON_US`, and
`REGIONS = STATES + NON_US`:

```python
State("", "JP-KANTO", "Kanto", 134.04, 18.62, 155.61, 37.16, "kanto"),
#      ^fips  ^scope   ^name   ^bbox (min_lon, min_lat, max_lon, max_lat)  ^Geofabrik basename
```

They are kept **out of `STATES` on purpose**: every builder's `--all` iterates
`STATES`, and a country-sized extract silently joining a 51-state run would be
a nasty surprise. Reach them explicitly with `--states JP-KANTO`.

**Derive bboxes from the extract's own PBF header — never by hand or from one
layer's extent.** Geofabrik's `kanto` reaches Minamitorishima at 155.6E and
`kyushu` reaches Yonaguni at 122.2E; a mainland-shaped guess clips them, and a
box fitted to the road network clips Ogasawara, which has no roads but does
have buildings:

```bash
uv run --with osmium python -c "
import osmium
b = osmium.io.Reader('/mnt/core/timeline-ptiles-cache/raw/kanto-latest.osm.pbf').header().box()
print(b.bottom_left.lon, b.bottom_left.lat, b.top_right.lon, b.top_right.lat)"
```

Extracts are located by `states.py:pbf_path(region, prefer=PBF_DIR)`, which
searches the three cache directories and both naming conventions
(`{name}.osm.pbf` and `{name}-latest.osm.pbf`). Pass `prefer` — those
directories hold different vintages, and a builder that has always read one of
them must keep reading it or its output silently changes snapshot.

### Granularity varies per layer

The US is uniform: one file per state per layer. Other countries need not be.
Japan's buildings are **eight regional files** because 29.5M buildings will not
fit one build (a single-file attempt OOMs; see the memory note below), while
its places, water, rail, parks and EV are one country-wide file each.

Geofabrik splits Japan into 8 regions, not the 47 prefectures:
`hokkaido tohoku kanto chubu kansai chugoku shikoku kyushu`.

Two consequences:

- **Region extracts overlap at their seams.** Summing buildings across the 8
  regions gives 29,476,617 against 29,457,618 counted on the whole-country
  extract — 0.06% duplication. That is expected, and matches how the Geofabrik
  US state extracts behave.
- **A client may hold several files for one layer**, so it must pick among
  them by bounds rather than assume one. This is a correctness matter, not just
  speed: a region that covers a point but holds no feature there must not
  overwrite a hit from a neighbouring region.

### Building Japan

```bash
# Country-wide layers (one file each)
uv run --with osmium --with h3 --with zstandard --with shapely \
    python scripts/build_places.py --states JP        # also parks, rail, trails, ev
uv run --with osmium --with h3 --with zstandard --with shapely \
    python scripts/build_water.py --source pbf --states JP
uv run --with osmium --with h3 --with zstandard --with shapely \
    python scripts/build_points.py --states JP        # signals + cameras

# Roads: takes a raw PBF path, no region table needed
uv run --with osmium --with h3 --with zstandard --with shapely \
    python scripts/build_roads.py \
    /mnt/core/timeline-ptiles-cache/raw/japan-latest.osm.pbf \
    /mnt/core/kino/ptiles/data/JP.roads.ptiles

# Buildings: per region, memory-capped so a runaway fails fast
for R in HOKKAIDO TOHOKU KANTO CHUBU KANSAI CHUGOKU SHIKOKU KYUSHU; do
  systemd-run --user --scope -p MemoryMax=10G -p MemorySwapMax=0 \
    uv run --with osmium --with h3 --with zstandard --with shapely \
    python scripts/build_state_v8.py JP-$R
done
```

**Memory.** Extraction holds every feature in RAM. Country-scale extracts need
the compact representation (`array('i')` of interleaved micro-degrees, ~8 bytes
per vertex against ~80 for a Python float tuple) that `build_roads.py` and
`build_state_v8.py` now use. Japan roads peaks at ~9 GB with it and OOMed at
14 GB without. Cap large runs with `systemd-run --scope -p MemoryMax=`, so a
runaway dies instead of pushing the box into zram — there is a resident LLM on
GPU 1 that must not get paged out.

### What is still US-only

| Layer          | Blocker                                    |
| -------------- | ------------------------------------------ |
| `admin`        | Census TIGER shapefiles                    |
| `address`      | NAD / OpenAddresses US shards              |
| `us_highways`  | US route-shield semantics; `build_roads` already covers other countries' `highway=*` |

Substitutes exist (OSM `admin_level` relations or GADM for admin; OSM `addr:*`
for addresses) but are not wired up.

### Reading and publishing

Clients take a scope or a country:

```python
PtilesClient.open_scope("JP-KANTO", data_dir)    # one scope
PtilesClient.open_country("JP", data_dir)        # every scope of a country
PtilesClient.from_manifest(manifest, data_dir, country="JP")
```

`open_country` uses a `manifest.json` beside the data when present and falls
back to probing filenames when absent — a partial download has no manifest. A
manifest that is present but unparseable raises rather than falling back, since
quietly probing would hide a corrupt publish. The TypeScript client mirrors all
of this (`openScope`, `openCountry`, `fromManifest`).

For the published layout (US at the snapshot root, other countries in a country
directory) see the R2 Upload section below and the README.

### Adding a new country

1. Download the extract(s) into `/mnt/core/timeline-ptiles-cache/raw/`.
2. Read each bbox from its PBF header and add rows to `NON_US` in `states.py`.
   If the region crosses the antimeridian, give it true wrapped bounds
   (`min_lon > max_lon`) — both the client and the build filter handle that.
3. If the ISO alpha-2 collides with a US state (see the 26 above), use the
   alpha-3 code. `states.py` will refuse the row otherwise.
4. Build the country-wide layers first; split a layer by subdivision only when
   it will not fit one build.
5. `python scripts/publish_snapshot.py <build_dir> <date> <source> --dry-run`
   and check the keys before uploading.

Layer versions are tracked per country, so a country rebuilt at
`buildings_v10` publishes alongside others still at `v9`. Two versions of one
layer *within* a country is still fatal — that filename is unpredictable.

### Cost of holding many countries

Readers open lazily: the header is read at open, the spatial index, zstd
dictionary and water's large-body table only on first query. Opening all of
Japan costs ~12 MB rather than the ~190 MB it did when everything was eager,
and a point query decodes the index of only the files whose bounds contain it
(2 of 8 building files for Tokyo). This is what makes holding a dozen countries
open practical, so keep new readers lazy — the pattern is in `ptiles/reader.py`.

`points.py` is the exception to nothing: it is lazy too. Roads files record
their bbox in the PTLR header at offset 84; files built before that field read
all-zero and are reported as bounds-unknown, which means every client consults
them on every query. Rebuild an old roads file to fix that.

### Known gaps

- **`ptiles/roads.py` cannot open PTLR at all** — not for Japan and not for the
  US. Roads has been unreadable by the Python client since the format changed.
- `trails` and `ev` have builders and published files but no reader in
  `LAYER_CONFIG`, so no client can open them.
- `build_business_name_index.py` tokenisation is untested on Japanese, which
  has no whitespace word breaks; prefix search there is suspect.

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

| Binary                  | Path                               | Purpose                                  |
| ----------------------- | ---------------------------------- | ---------------------------------------- |
| `ptiles`                | `src/main.rs`                      | General query CLI for all layers         |
| `routing-index-builder` | `src/bin/routing_index_builder.rs` | Build .routing.ptiles from .roads.ptiles |
| `routing-debug`         | `src/bin/routing_debug.rs`         | Debug portal detection per cell          |
| `scan-portals`          | `src/bin/scan_portals.rs`          | Calibrate portal distance threshold      |

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

| Byte   | ASCII | Layer                      |
| ------ | ----- | -------------------------- |
| `0x46` | `F`   | Buildings (footprints)     |
| `0x52` | `R`   | Roads                      |
| `0x41` | `A`   | Admin boundaries           |
| `0x57` | `W`   | Water                      |
| `0x50` | `P`   | Places                     |
| `0x4E` | `N`   | Parks                      |
| `0x54` | `T`   | Rail/transit               |
| `0x49` | `I`   | POIs                       |
| `0x44` | `D`   | Address ranges             |
| `0x55` | `U`   | Routing (companion format) |

## h3-py v4 API Quirks

The `h3` Python library v4 renamed several functions. Scripts in this repo
target the v4 API:

| Old (v3)                      | New (v4)                           |
| ----------------------------- | ---------------------------------- |
| `h3.geo_to_h3(lat, lon, res)` | `h3.latlng_to_cell(lat, lon, res)` |
| `h3.h3_to_geo(cell)`          | `h3.cell_to_latlng(cell)`          |

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

Contains cached PBFs (Jan 16) and admin shapefiles. The NFS cache at
`timeline-ptiles-cache/raw/` has all 53 state PBFs. The repo `data/pbfs/`
is an older partial mirror.

## Zstd Dictionary Training

For the roads layer, train a zstd dictionary on the first ~10,000 blocks
before final compression. For other layers, the first ~500-1000 blocks.
Dictionary training reduces block size by ~30%.

## R2 Upload

Publish a whole build as a dated snapshot, with its manifest:

```bash
python3 scripts/publish_snapshot.py <build_dir> 2026-08-20 osm-2026-08-07 --dry-run
python3 scripts/publish_snapshot.py <build_dir> 2026-08-20 osm-2026-08-07
```

It derives every key from `ptiles.scopes.publish_relpath`, generates the
manifest from the same tree, and refuses to upload if the two disagree — a file
uploaded but missing from the manifest is invisible to clients.

**Layout:** the US stays at the snapshot root (those URLs are already published
and read by clients outside this repo); every other country gets a directory.

```
maps/2026-08-20/manifest.json
maps/2026-08-20/TN.buildings_v9.ptiles
maps/2026-08-20/JP/JP-KANTO.buildings_v9.ptiles
```

Single-file uploads still work, but the key must carry the same layout:

```bash
AWS_PROFILE=mdt-r2 aws s3 cp data/states/TN.buildings_v8.ptiles \
    s3://mydatatimeline/maps/2026-08-20/TN.buildings_v8.ptiles
```

**Profile:** `mdt-r2` (configured in `~/.aws/config` and `~/.aws/credentials`)
**Bucket:** `mydatatimeline` (contains `downloads/`, `maps/`, `models/`)

**Scopes:** a bare two-letter scope is a US state; other countries are named
directly (`JP`) and split by subdivision when a layer will not fit one file
(`JP-KANTO`). Granularity varies per layer, so clients must read `manifest.json`
rather than assume one file per layer. See `ptiles/scopes.py`.

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
