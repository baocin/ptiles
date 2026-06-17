# PTILES Building → Business Enrichment

## Design Decision: Hybrid (Option 2 variant + Option 3)

**Chosen approach:** H3-cell-partitioned sidecar `.building_business.ptiles` 
file that maps building OSM IDs to business records, organized by the same 
H3 res-7 cells as the buildings file. No changes to the building record 
format itself.

**Why not pure Option 1 (full spatial re-join at runtime)?**
Runtime spatial join (building polygon.contains(business point)) requires 
both building geometry AND business data loaded for every candidate cell, 
and polygon-in-point tests are costly. Offline pre-computation is better.

**Why not pure Option 2 (all data in building record)?**
The building v8 format is tight (~15 bytes/building avg). Adding business 
contact fields to 20% of buildings would bloat records. Separate file = 
independent compressibility, no format changes, independent update cycles.

**Why not pure Option 3 (standalone business file)?**
Existing per-state `.business.ptiles` files are ~975 MB US-wide and store 
ALL businesses. A building-attached sidecar is smaller (~300 MB) and 
enables "given this building, what businesses are inside?" without re-running 
spatial queries.

---

## (a) Building Record Format Changes

**None.** The building v8 record format is unchanged:

- `poi_osm_id` (existing flags2 bit 3, uint64) already links a building 
  to a single OSM POI on ~1% of buildings. This is preserved as-is.
- The sidecar handles the multi-business case using OSM ID as the key 
  within each H3 cell.

Rationale: zero format change = zero Rust decoder changes for existing files.
The building file stays at 1.14 GB with no bloat.

---

## (b) Sidecar File Format: `.building_business.ptiles`

### Naming Convention

Per-state sidecar files alongside existing building files:

```
data/states/TN.buildings_v8.ptiles        # existing, 7.4 MB
data/states/TN.building_business.ptiles   # new, ~6-10 MB
```

**Magic:** `PTILESL\0` — 'L' for Linked/Location business. This lets the 
Rust reader distinguish at the binary level without relying on file extensions.

### Header (256 bytes)

Standard PTILES header (SPEC.md / shared.py). Version = 1.

| Field | Meaning |
|-------|---------|
| magic | `PTILESL\0` |
| version | 1 |
| feature_count | Total number of buildings that have ≥1 business |
| block_count | Number of H3 cell blocks (merged v2-style) |

### Spatial Index

**19-byte entries** (same as v1 buildings index), sorted by H3 cell ID:

```
entry_count  u32

entry (19 bytes):
  u64  h3_cell             — H3 resolution 7 cell
  u48  block_offset        — absolute byte offset (or relative for per-state)
  u24  block_length         — compressed block size
  u16  entry_count          — number of building→business entries in block
```

The index covers the SAME H3 cells as the buildings file. Cells with 
buildings but no businesses are absent from this index.

### Data Block Format (per H3 cell, zstd compressed)

After decompression:

```
# Per-cell mini-header
u32  entry_count                  — number of buildings with businesses in this cell
i32  center_lon_micro             — u16 coord encoding center (from merged block)
i32  center_lat_micro

# Building lookup table (sorted by osm_id for binary search)
for each of entry_count entries:
  u64  building_osm_id            — OSM way ID
  u32  record_offset              — relative offset from start of record_data
  u16  business_count             — number of businesses (0 = just closed)

# Record data
for each entry (same order):
  for each of business_count businesses:
    u32  record_len
    record_data:
      i16 lon_offset              — cell-relative u16 lon (bias-32768 from center)
      i16 lat_offset              — cell-relative u16 lat
      u16_str name                — business name (uint16 len prefix)
      u8   category_idx           — 1-based index into sidecar categories JSON
      u8   flags
        0x01: has_phone
        0x02: has_website
        0x04: has_address
        0x08: has_brand
        0x10: is_closed
        0x12: is_temporarily_closed
        0x20: has_emails
        0x40: has_socials
      optional fields (per flags, same encoding as existing business format)
```

Within a block, both the building lookup table and the business records 
share the same cell-relative u16 coordinate center for location encoding.

### Category Sidecar

Reuses existing pattern from `build_us_business.py`:

```
TN.building_business_categories.json
{"categories": ["restaurant", "cafe", "church_cathedral", ...]}
```

---

## (c) Build Pipeline

### Overview: Two-pass pipeline

```
Pass 1: Spatial join  —  Overture Places × building footprints  →  temp JSONL
Pass 2: Encode        —  temp JSONL  →  .building_business.ptiles
```

### Pass 1: Spatial Join

**Script:** `build_building_business.py` (first half)

**Inputs:**
1. Overture Places Parquet (9.7 GB, 16 files at `~/overture-2026-04-15.0/places/`)
2. Building footprints decoded from per-state `.buildings_v8.ptiles` file
   OR: building polygons from per-state OSM PBF if rebuilding from scratch

**Strategy — Reuse existing artifacts:**
- `build_us_business.py` Pass 1 already produces per-state temp files 
  at `/tmp/ptiles_business_v1/{STATE}.jsonl` — these contain ALL US 
  businesses by state with {id, lon, lat, name, cat, addr, phone, web, brand}
- Decode the v8 building file to get building polygons per state

**Steps:**
1. Load state's building polygons from `.buildings_v8.ptiles` by 
   iterating H3 blocks, decompressing, parsing building records.
2. Read state's business temp JSONL.
3. Build R-tree (shapely.STRtree) on building polygons.
4. For each business point: query R-tree for containing polygon.
5. Group matched businesses by building_osm_id.
6. Write temp file: `building_osm_id, [businesses...]`

**Runtime:**
- Decode buildings file: ~10-30s per state
- R-tree build + query: ~2-5 min per state (avg ~50k businesses, ~100k buildings)
- Total all 48 states: ~2-4 hours (easily parallelizable)

**Memory:** ~1-3 GB per state (decoded polygons + business list).

### Pass 2: Encode

**Script:** `build_building_business.py` (second half)

**Steps:**
1. Read temp file: list of (building_osm_id, [business_dict, ...])
2. Group by H3 res-7 cell (using building centroid or known cell)
3. For each cell:
   a. Sort by building_osm_id
   b. Encode building lookup table
   c. Encode business records with cell-relative u16 coords
   d. Build per-cell mini-block (for solo or merged)
4. Merge sparse cells using existing v2 merged-block code in shared.py
5. Train zstd dictionary on sample of raw blocks
6. Compress all blocks
7. Write file: header → dict → index → blocks

**Runtime:** ~2-5 min per state. Total ~2 hours all states.

**Memory:** < 2 GB per state (temp file has only maching businesses, ~20% of total).

### Build Commands

```bash
# Target single state
uv run --with pyarrow --with shapely --with h3 --with zstandard --with numpy --with 'polars>=1.0' \
    python scripts/build_building_business.py TN

# All 48 states
uv run --with pyarrow --with shapely --with h3 --with zstandard --with numpy --with 'polars>=1.0' \
    python scripts/build_building_business.py --all
```

---

## (d) Runtime Query

### Lat/Lon Query Flow

```
Given (lat, lon):

1. Convert lat/lon → H3 res-7 cell
2. Query .buildings_v8.ptiles index:
   - Binary search for H3 cell entry
   - Seek to compressed block, decompress
   - For each building record:
       if polygon.contains(point):
         return building (with osm_id)

3. If building found, with its osm_id:
   Query .building_business.ptiles index:
   - Same H3 cell → compressed block offset
   - Decompress block
   - Binary search building_lookup_table for osm_id
   - If found: read record_offset → decode business records

4. Return: { building, businesses: [...] }
```

### Rust Reader: `linked_business.rs`

Struct `BuildingBusinessReader` extends the pattern from `business.rs`:

```rust
pub struct BuildingBusinessReader {
    file: File,
    header: Header,
    index: Index,           // H3 → block offset (19-byte v1 entries)
    dict_data: Vec<u8>,
    categories: Vec<String>,
    relative_offsets: bool,
}

impl BuildingBusinessReader {
    /// Open sidecar file + auto-load categories JSON sidecar
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self>;

    /// Query: given a building OSM ID + the H3 cell it's in,
    /// return all business records for that building.
    pub fn get_businesses(
        &mut self, 
        building_osm_id: u64, 
        cell: CellIndex
    ) -> Result<Vec<Business>>;

    /// Get ALL building→business entries in a cell
    pub fn get_all_in_cell(&mut self, cell: CellIndex) 
        -> Result<Vec<(u64, Vec<Business>)>>;
}
```

### Composite Query Integration (`composite.rs`)

```rust
pub struct PointReport {
    pub building: Option<Building>,
    pub businesses: Vec<Business>,     // NEW: businesses in the found building
    // ... existing fields
}

pub struct PtilesClient {
    pub buildings: Option<BuildingsReader>,
    pub building_business: Option<BuildingBusinessReader>,  // NEW
    // ... existing fields
}

impl PtilesClient {
    pub fn open_state(state: &str, data_dir: &Path) -> Result<Self> {
        // Open buildings file (existing)
        // Open building business sidecar if it exists
    }

    pub fn query_point(&mut self, lat: f64, lon: f64) -> Result<PointReport> {
        // 1. Query buildings (existing)
        // 2. If building found, query sidecar (new)
        // 3. Return combined result
    }
}
```

### File Size Summary

| File | US Total | Per State (avg) |
|------|----------|------------------|
| `*.buildings_v8.ptiles` | 1.14 GB | 24 MB |
| `*.building_business.ptiles` | **~300 MB** | **~6 MB** |
| `*_categories.json` (sidecar) | ~200 KB | ~4 KB |

**Total additional: ~300 MB.** Well within constraint.

---

## Implementation Details

### Block Merge Strategy

Sparse H3 cells (≤100 buildings with businesses) are merged into shared 
blocks using the existing v2 merged-block encoding in `shared.py`:

- Merged block header: `center_lon_micro, center_lat_micro, cell_count`
- Cell table: `u64 cell_id, u32 record_offset` per cell
- Record data: concatenated per-cell business records

Dense cells (>100) get their own block. Threshold configurable.

### Spatial Join Optimization

For states with large building counts (CA: ~10M, TX: ~8M), the R-tree 
query is the bottleneck. Optimization:

1. Pre-filter businesses by building bounding box (quick rejection)
2. Use prepared geometry (shapely.prep) for contains() tests
3. Batch query: `tree.query(batch_of_points)` reduces Python overhead

### Edge Cases

- **Building with 0 businesses after spatial join:** Not written to sidecar.
- **Business on property boundary:** Uses `.contains()` not `.within()` — 
  a point on the boundary of a single building is considered inside.
- **Closed businesses:** Included (has `is_closed` flag). Consumer filters.
- **Building with no geometry in v8 file:** Skipped during decode.
