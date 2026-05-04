# Building PTiles

How to build .ptiles files from PMTiles source data.

## Prerequisites

### System Requirements

- Linux or macOS (CI runs on `ubuntu-latest`)
- ~200 GB free disk space (for PMTiles source extract + output files)
- 8+ GB RAM recommended

### Rust Toolchain

Install Rust via [rustup](https://rustup.rs):

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env
```

The `ptiles-v7` crate uses Rust edition 2021 with these dependencies:

| Crate | Version | Purpose |
|-------|---------|---------|
| byteorder | 1.5 | Little-endian binary I/O |
| thiserror | 2 | Error type derivation |
| zstd | 0.13 | Dictionary compression |
| h3o | 0.7 | H3 spatial indexing |

### Python 3.12+

The build scripts require Python 3.12 or later. Install dependencies:

```bash
pip install h3 zstandard shapely pmtiles requests
```

Or from the requirements file:

```bash
pip install -r scripts/requirements.txt
```

Package details:

| Package | Version | Purpose |
|---------|---------|---------|
| h3 | >=4.0 | H3 cell computation |
| zstandard | >=0.22 | Zstd compression/decompression |
| shapely | >=2.0 | Polygon operations (point-in-polygon) |
| pmtiles | >=0.1 | PMTiles format reader |
| requests | >=2.31 | HTTP downloads |

### PMTiles Source Data

You need a PMTiles extract covering the geographic region you want to build. The build script has been tested with Protomaps US extracts.

The CI pipeline defaults to `https://build.protomaps.com/20250414.pmtiles` (~130 GB for the full US). You can use any PMTiles URL by passing `--input` to the build script.

## Build Process

### Step 1: Clone the Repository

```bash
git clone http://localhost:3001/kino/ptiles.git
cd ptiles
```

The repository uses Git LFS for `.ptiles` files. Make sure LFS is installed:

```bash
git lfs install
git lfs pull
```

### Step 2: Build the Rust Crate

The `ptiles-v7` crate provides the schema types and binary reader used by the Python build scripts.

```bash
cd ptiles-v7
cargo build --release
cargo test --release
```

Expected output:

```
running 5 tests
test header::tests::test_read_header ... ok
test header::tests::test_bad_magic ... ok
test header::tests::test_bad_version ... ok
test index::tests::test_read_index ... ok
test index::tests::test_binary_search_midpoint ... ok
test layer::tests::test_layer_type_roundtrip ... ok
test layer::tests::test_read_layer_dir ... ok
test varint::tests::test_varint_roundtrip ... ok
test varint::tests::test_zigzag_roundtrip ... ok
test varint::tests::test_varint_known ... ok

test result: ok. 10 passed; 0 failed
```

Note: The `ptiles-v7` crate compiles but is a library. The actual building is done by Python scripts.

### Step 3: Download PMTiles Source

Download the source PMTiles extract:

```bash
mkdir -p data
curl -L --retry 3 --retry-delay 30 \
  -o data/source.pmtiles \
  "https://build.protomaps.com/20250414.pmtiles"
```

This is a large download (~130 GB for the full US). It can take several hours depending on your connection. Verify the download:

```bash
ls -lh data/source.pmtiles
```

### Step 4: Build .ptiles Files

Run the build script for each layer. The build script (`scripts/build_ptiles_footprints.py`) accepts these arguments:

| Flag | Description | Example |
|------|-------------|---------|
| `--input` | Path to source PMTiles file | `data/source.pmtiles` |
| `--layer` | Layer name to build | `buildings`, `roads`, `places`, `admin`, `water`, `rail`, `parks` |
| `--output` | Output .ptiles file path | `output/US.buildings.ptiles` |
| `--schema` | Schema version (v6 or v7) | `v7` |

#### Building individual layers

```bash
mkdir -p output

# Buildings (~1.14 GB)
python scripts/build_ptiles_footprints.py \
  --input data/source.pmtiles \
  --layer buildings \
  --output output/US.buildings.ptiles \
  --schema v7

# Roads
python scripts/build_ptiles_footprints.py \
  --input data/source.pmtiles \
  --layer roads \
  --output output/US.roads.ptiles \
  --schema v7

# Places
python scripts/build_ptiles_footprints.py \
  --input data/source.pmtiles \
  --layer places \
  --output output/US.places.ptiles \
  --schema v7

# Admin boundaries
python scripts/build_ptiles_footprints.py \
  --input data/source.pmtiles \
  --layer admin \
  --output output/US.admin.ptiles \
  --schema v7

# Water bodies
python scripts/build_ptiles_footprints.py \
  --input data/source.pmtiles \
  --layer water \
  --output output/US.water.ptiles \
  --schema v7

# Railways
python scripts/build_ptiles_footprints.py \
  --input data/source.pmtiles \
  --layer rail \
  --output output/US.rail.ptiles \
  --schema v7

# Parks and protected areas
python scripts/build_ptiles_footprints.py \
  --input data/source.pmtiles \
  --layer parks \
  --output output/US.parks.ptiles \
  --schema v7
```

#### Building all layers (loop)

```bash
for layer in buildings roads places admin water rail parks; do
    echo "=== Building US.${layer}.ptiles ==="
    python scripts/build_ptiles_footprints.py \
      --input data/source.pmtiles \
      --layer "$layer" \
      --output "output/US.${layer}.ptiles" \
      --schema v7
done
```

### Step 5: Verify Output

```bash
ls -lh output/
```

Expected output files:

- `US.buildings.ptiles` -- ~1.14 GB (77M+ buildings)
- `US.roads.ptiles` -- Varies by region
- `US.places.ptiles` -- Varies by region
- `US.admin.ptiles` -- Varies by region
- `US.water.ptiles` -- Varies by region
- `US.rail.ptiles` -- Varies by region
- `US.parks.ptiles` -- Varies by region

### Step 6: Read / Query .ptiles Files

Use the Rust reader to query features:

```rust
use ptiles_v7::{PtilesReader, LayerType};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut reader = PtilesReader::open("output/US.buildings.ptiles")?;

    // Print header info
    let h = reader.header();
    println!("Version: {}, Layers: {}", h.version, h.layer_count);
    println!("Features: {}", h.total_poi_count);
    println!("Bounds: ({}, {}) to ({}, {})", h.min_lat, h.min_lon, h.max_lat, h.max_lon);

    // Query a specific H3 cell
    let h3_cell = 0x0870c00051ffffffu64;
    let features = reader.query_cell(LayerType::Buildings, h3_cell)?;
    println!("Found {} buildings in cell", features.len());

    Ok(())
}
```

## Build Time Estimates

| Stage | Duration | Notes |
|-------|----------|-------|
| Rust build | 2-5 min | First build downloads crates |
| PMTiles download | 1-4 hours | Depends on connection speed |
| Building layer | 10-30 min | Per layer, varies by density |
| **Total (all layers)** | **4-8 hours** | CI timeout: 480 minutes |

## Troubleshooting

### "No such file or directory: build_ptiles_footprints.py"

The build script (`scripts/build_ptiles_footprints.py`) must exist. If it is not yet committed, it may need to be created.

### "MemoryError" or "Killed"

PMTiles extracts are very large (~130 GB). Ensure you have enough RAM and swap. Consider using a machine with 16+ GB RAM.

### "zstd decompression error"

The .ptiles file may be corrupted or uses an incompatible dictionary. Verify the file was built with the same dictionary as the reader.

### "H3 cell not found in index"

The queried H3 cell has no features for that layer. This is normal for sparse layers like admin boundaries or water bodies.

### "failed to read Username for 'http://localhost:3001'"

Configure git credentials for the Gitea instance:

```bash
git config --global credential.helper store
# Clone with username:token in URL
git clone http://kino:YOUR_TOKEN@localhost:3001/kino/ptiles.git
```

### LFS files not downloading

```bash
git lfs install
git lfs pull
```

### Python import errors

```bash
pip install --upgrade pip
pip install h3 zstandard shapely pmtiles requests
```

Verify installation:

```bash
python -c "import h3; import zstandard; import shapely; import pmtiles; print('OK')"
```

## CI Automation

For automated builds, see `.github/workflows/build-ptiles.yml` and `docs/CI.md`. The CI pipeline handles all steps automatically on `ubuntu-latest` runners.
