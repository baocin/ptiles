# CI / CD Pipeline

The PTiles build pipeline is defined in `.github/workflows/build-ptiles.yml`.

## Overview

The pipeline builds .ptiles files from PMTiles source data through 7 stages. It runs on `ubuntu-latest` with an 8-hour timeout.

## Triggers

### Manual (workflow_dispatch)

Trigger from the GitHub/Gitea Actions tab with configurable inputs:

| Input | Description | Default |
|-------|-------------|---------|
| `pmtiles_url` | URL to source PMTiles extract | `""` (falls back to default) |
| `layers` | Comma-separated layers to build | `"buildings,roads,places"` |
| `release_tag` | Tag for GitHub Release (empty = skip) | `""` |

### Scheduled (cron)

Runs automatically every Sunday at 06:00 UTC:

```yaml
on:
  schedule:
    - cron: "0 6 * * 0"
```

Scheduled runs use all 7 layers (from `DEFAULT_LAYERS` env var).

## Environment Variables

```yaml
env:
  CARGO_TERM_COLOR: always
  RUST_BACKTRACE: 1
  DEFAULT_LAYERS: "buildings,roads,places,admin,water,rail,parks"
  DEFAULT_PMTILES_URL: "https://build.protomaps.com/20250414.pmtiles"
```

## Pipeline Stages

### Stage 1: Checkout (actions/checkout@v4)

```yaml
- name: Checkout repository
  uses: actions/checkout@v4
  with:
    lfs: true
```

Clones the repository with Git LFS objects.

### Stage 2: Rust Toolchain (dtolnay/rust-toolchain)

```yaml
- name: Install Rust toolchain
  uses: dtolnay/rust-toolchain@stable
  with:
    toolchain: stable
    components: clippy, rustfmt

- name: Cache Rust dependencies
  uses: Swatinem/rust-cache@v2
  with:
    workspaces: ptiles-v7

- name: Build ptiles-v7 crate
  run: cargo build --release
  working-directory: ptiles-v7

- name: Run Rust tests
  run: cargo test --release
  working-directory: ptiles-v7
```

Installs stable Rust, caches dependencies, builds the `ptiles-v7` crate, and runs all 10 tests.

### Stage 3: Python Environment (actions/setup-python@v5)

```yaml
- name: Install Python
  uses: actions/setup-python@v5
  with:
    python-version: "3.12"

- name: Cache Python dependencies
  uses: actions/cache@v4
  with:
    path: ~/.cache/pip
    key: pip-${{ runner.os }}-${{ hashFiles('scripts/requirements.txt') }}
    restore-keys: |
      pip-${{ runner.os }}-

- name: Install Python dependencies
  run: |
    pip install --upgrade pip
    pip install h3 zstandard shapely pmtiles requests
```

Sets up Python 3.12, caches pip packages, installs h3, zstandard, shapely, pmtiles, and requests.

### Stage 4: Download PMTiles (timeout: 240 minutes)

```yaml
- name: Download PMTiles extract
  run: |
    PMTILES_URL="${{ github.event.inputs.pmtiles_url || env.DEFAULT_PMTILES_URL }}"
    echo "Downloading PMTiles from: $PMTILES_URL"
    mkdir -p data
    curl -L --retry 3 --retry-delay 30 -o data/source.pmtiles "$PMTILES_URL"
    ls -lh data/source.pmtiles
  timeout-minutes: 240
```

Downloads the PMTiles extract with retries. Uses the manual input URL if provided, otherwise falls back to `DEFAULT_PMTILES_URL`.

### Stage 5: Build Layers (timeout: 180 minutes)

```yaml
- name: Build .ptiles layers
  run: |
    LAYERS="${{ github.event.inputs.layers || env.DEFAULT_LAYERS }}"
    echo "Building layers: $LAYERS"
    mkdir -p output

    IFS=',' read -ra LAYER_ARRAY <<< "$LAYERS"
    for layer in "${LAYER_ARRAY[@]}"; do
      layer=$(echo "$layer" | xargs)
      echo ""
      echo "=== Building US.${layer}.ptiles ==="
      python scripts/build_ptiles_footprints.py \
        --input data/source.pmtiles \
        --layer "$layer" \
        --output "output/US.${layer}.ptiles" \
        --schema v7
    done

    echo ""
    echo "=== Build complete ==="
    ls -lh output/
  timeout-minutes: 180
```

Iterates over the selected layers and calls `build_ptiles_footprints.py` for each, producing `output/US.{layer}.ptiles`.

### Stage 6: Upload Artifacts (actions/upload-artifact@v4)

```yaml
- name: Upload .ptiles artifacts
  uses: actions/upload-artifact@v4
  with:
    name: ptiles-output
    path: output/*.ptiles
    retention-days: 90
    compression-level: 0  # already zstd compressed
```

Uploads all `.ptiles` output files as a workflow artifact. Retention: 90 days. Compression disabled because files are already zstd-compressed.

### Stage 7: GitHub Release (optional)

```yaml
- name: Create GitHub Release
  if: github.event_name == 'workflow_dispatch' && github.event.inputs.release_tag != ''
  uses: softprops/action-gh-release@v2
  with:
    tag_name: ${{ github.event.inputs.release_tag }}
    name: "PTiles ${{ github.event.inputs.release_tag }}"
    body: |
      PTiles build from PMTiles extract.
      **Source URL**: ...
      **Layers built**: ...
      **Schema version**: v7
      **Build run**: ...
    files: output/*.ptiles
    draft: false
    prerelease: false
```

Only runs on manual dispatch with a `release_tag` provided. Creates a GitHub Release with all output files attached.

## How to Run Manually

### Via GitHub/Gitea UI

1. Go to the repository Actions tab
2. Select "Build .ptiles" workflow
3. Click "Run workflow"
4. Fill in inputs:
   - `pmtiles_url`: (leave empty for default)
   - `layers`: `buildings,roads,places` (or comma-separated list)
   - `release_tag`: `v7.0.1` (or leave empty to skip release)
5. Click "Run workflow"

### Via CLI

If using GitHub CLI:

```bash
gh workflow run build-ptiles.yml \
  -f layers="buildings,roads,places" \
  -f release_tag="v7.0.1"
```

## Artifact Locations

- **Workflow artifacts**: GitHub/Gitea Actions UI > workflow run > Artifacts section
- **URL format**: `https://github.com/{owner}/{repo}/actions/runs/{run_id}`
- **Retention**: 90 days from build date
- **GitHub Releases**: `https://github.com/{owner}/{repo}/releases/tag/{tag}`

## Release Process

1. Trigger workflow_dispatch with a `release_tag` (e.g. `v7.0.1`)
2. All layers build and pass
3. Artifacts upload with 90-day retention
4. GitHub Release created automatically with:
   - Tag: the provided `release_tag`
   - Name: "PTiles {tag}"
   - Body: source URL, layers built, schema version, build run link
   - Files: all `output/*.ptiles` attached

## Timeouts

| Stage | Timeout |
|-------|---------|
| PMTiles download | 240 minutes (4 hours) |
| Layer build | 180 minutes (3 hours) |
| Entire workflow | 480 minutes (8 hours) |

## Failure Modes

### PMTiles download fails

The `curl` command retries 3 times with 30-second delays. If all retries fail, the workflow fails at Stage 4. Check the PMTiles URL is valid and accessible.

### Layer build fails

The build script (`scripts/build_ptiles_footprints.py`) must exist and be functional. If a specific layer fails, the workflow will stop. Check the build log for Python tracebacks.

### Rust build fails

Check that the Rust toolchain is stable and that all crate dependencies (byteorder, thiserror, zstd, h3o) are available. The rust-cache action may need to be cleared if there are cache corruption issues.

### Artifact upload fails

Ensure `output/` contains files matching `*.ptiles`. The upload step uses `compression-level: 0` because files are already zstd-compressed. If the upload fails, check GitHub's artifact size limits.
