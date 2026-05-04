# Repository Structure

Overview of the PTiles Gitea fork: layout, remotes, and how to sync with upstream.

## Repository Layout

```
ptiles/
├── .github/
│   └── workflows/
│       └── build-ptiles.yml          # CI pipeline (7-stage build)
├── docs/
│   ├── SCHEMA-v7.md                  # v7 binary format specification
│   ├── BUILDING.md                   # Build instructions
│   ├── REPO-STRUCTURE.md             # This file
│   └── CI.md                         # CI workflow documentation
├── ptiles-v7/                        # Rust crate (v7 schema + reader)
│   ├── Cargo.toml                    # Crate manifest (ptiles-v7 7.0.0)
│   ├── .gitignore                    # target/, Cargo.lock
│   └── src/
│       ├── lib.rs                    # Crate root, public API
│       ├── header.rs                 # 512-byte header (magic, bounding box, offsets)
│       ├── layer.rs                  # Layer types, geometry types, layer directory
│       ├── index.rs                  # Spatial index (H3 cell -> block offset)
│       ├── reader.rs                 # High-level PtilesReader API
│       ├── varint.rs                 # Protobuf varint + zigzag encoding
│       └── records/
│           ├── mod.rs                # Shared decode_coordinates helper
│           ├── buildings.rs          # Layer 0: Building record decoder
│           ├── roads.rs              # Layer 1: Road record decoder
│           ├── places.rs             # Layer 2: Place record decoder
│           ├── admin.rs              # Layer 3: Admin boundary decoder
│           ├── water.rs              # Layer 4: Water body decoder
│           ├── rail.rs               # Layer 5: Railway decoder
│           └── parks.rs              # Layer 6: Park decoder
├── scripts/
│   └── requirements.txt              # Python dependencies for build pipeline
├── .gitattributes                     # LFS tracking for *.ptiles
├── .gitignore                         # Ignored patterns
├── README.md                          # Project overview
├── US.ptiles                          # v6 US buildings file (LFS, ~1.14 GB)
├── US.ptiles.schema.v6                # v6 binary format specification
└── US.ptiles.schema.v7                # v7 binary format specification
```

## Key Components

### ptiles-v7/ (Rust Crate)

The core v7 schema implementation and binary reader library.

- **Package**: `ptiles-v7` v7.0.0
- **License**: MIT
- **Module structure**: 7 modules (header, layer, index, reader, varint, records)
- **Tests**: 10 tests covering header parsing, index binary search, varint roundtrips, and layer directory parsing

```bash
cd ptiles-v7
cargo build --release
cargo test --release
```

### scripts/

Python scripts for building .ptiles files from PMTiles source data.

- `requirements.txt` -- Python dependencies (h3, zstandard, shapely, pmtiles, requests)
- `build_ptiles_footprints.py` -- Main build script (referenced by CI pipeline and README)

### .github/workflows/

GitHub Actions CI pipeline.

- `build-ptiles.yml` -- 7-stage pipeline: checkout, Rust, Python, download, build, upload, release

### Schema Files

- `US.ptiles.schema.v6` -- v6 format (buildings only, 77M+ footprints)
- `US.ptiles.schema.v7` -- v7 format (7-layer multi-type container)

### Data File

- `US.ptiles` -- v6 US buildings file (~1.14 GB, stored via Git LFS)

## Remote Configuration

The repository has two remotes:

| Remote | URL | Purpose |
|--------|-----|---------|
| origin | `http://localhost:3001/kino/ptiles` | Gitea fork (primary) |
| upstream | `https://github.com/baocin/ptiles` | Original upstream repository |

### Checking Remotes

```bash
git remote -v
# origin    http://localhost:3001/kino/ptiles (fetch)
# origin    http://localhost:3001/kino/ptiles (push)
# upstream  https://github.com/baocin/ptiles (fetch)
# upstream  https://github.com/baocin/ptiles (push)
```

### Adding the Upstream Remote

If not already configured:

```bash
git remote add upstream https://github.com/baocin/ptiles
```

## Syncing with Upstream

### Fetch upstream changes

```bash
git fetch upstream
```

### Merge upstream into main

```bash
git checkout main
git merge upstream/main
```

Resolve any conflicts, then push to Gitea:

```bash
git push origin main
```

### Rebase workflow (alternative)

```bash
git checkout main
git fetch upstream
git rebase upstream/main
git push origin main --force-with-lease
```

Use `--force-with-lease` carefully -- only when you are the sole contributor to the branch.

## Git LFS

The repository uses Git LFS for `.ptiles` binary files.

### Setup

```bash
# Install Git LFS (if not already installed)
# Linux: download from https://github.com/git-lfs/git-lfs/releases
wget https://github.com/git-lfs/git-lfs/releases/download/v3.7.1/git-lfs-linux-amd64-v3.7.1.tar.gz
tar xzf git-lfs-linux-amd64-v3.7.1.tar.gz
sudo install git-lfs-3.7.1/git-lfs /usr/local/bin/git-lfs

# Initialize
git lfs install
```

### LFS-tracked files

From `.gitattributes`:

```
*.ptiles filter=lfs diff=lfs merge=lfs -text
```

### Pulling LFS objects

```bash
git lfs pull
```

## Branch Strategy

- `main` -- Primary development branch, synced with upstream
- `v7.0.0` -- Release tag for the v7 schema crate

## Gitea Instance

- **URL**: http://localhost:3001
- **User**: kino
- **Repository**: `kino/ptiles`
- **Visibility**: Private (requires authentication)

### Access Token

Generate an access token via the Gitea admin CLI:

```bash
gitea admin user generate-access-token \
  -u kino \
  -t "token-name" \
  --raw \
  --scopes "read:repository,write:repository" \
  -c /home/aoi/gitea/custom/conf/app.ini
```

Use the token in git URLs:

```bash
git clone http://kino:TOKEN@localhost:3001/kino/ptiles.git
```
