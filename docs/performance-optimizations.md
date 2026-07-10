# PTILES Performance Optimization Guide

## Current file sizes (Tennessee)

| Layer     | File size  | Version | Blocks     | Features      | Index entries |
| --------- | ---------- | ------- | ---------- | ------------- | ------------- |
| Roads     | 32.6 MB    | v2      | 23,087     | 1,190,884     | 23,087        |
| Business  | 53.9 MB    | v3      | 18,162     | 829,528       | 18,162        |
| Water     | 15.4 MB    | v1      | 14,550     | 151,594       | 14,550        |
| Buildings | 16.2 MB    | v8      | 7,893      | 937,709       | 7,893         |
| Places    | 0.5 MB     | v1      | 652        | 6,996         | 652           |
| Parks     | 0.4 MB     | v1      | 208        | 4,042         | 208           |
| Rail      | 2 KB       | v1      | 2          | 56            | 2             |
| **Total** | **119 MB** |         | **66,554** | **3,120,809** |               |

---

## Table of Contents

1. [Zoom-band chunk merging (v3 format)](#1-zoom-band-chunk-merging-v3-format)
2. [Search indexes](#2-search-indexes)
3. [DDict caching in workers](#3-ddict-caching-in-workers)
4. [Already deployed](#4-already-deployed)
5. [Implementation order](#5-implementation-order)

---

## 1. Zoom-band chunk merging (v3 format)

### Problem

Each PTILES file stores one ZSTD frame per H3 cell block. For roads, that's 23,087 individual ZSTD decompressions per scroll. 85% of blocks are under 1KB — framing overhead dominates (14-22 bytes per frame = 3-10% of payload). Per-frame dict re-initialization adds ~100µs per block.

### Format change

Replace per-cell ZSTD frames with zoom-band chunks. Each chunk is a single ZSTD frame containing all blocks for that zoom level. Three bands:

| Band | H3 res | Cells per band (TN roads) | Zoom range | Purpose                                  |
| ---- | ------ | ------------------------- | ---------- | ---------------------------------------- |
| Z04  | res 4  | ~180                      | 5-9        | State-wide overview, major highways only |
| Z05  | res 5  | ~1,100                    | 10-12      | County view, all roads simplified        |
| Z07  | res 7  | ~23,087                   | 13+        | Street-level detail, full geometry       |

### Cost estimate for roads (TN)

| Band      | Blocks  | Raw decompressed | ZSTD compressed | Ratio |
| --------- | ------- | ---------------- | --------------- | ----- |
| Z04       | ~180    | ~1 MB            | **~0.3 MB**     | 3:1   |
| Z05       | ~1,100  | ~6 MB            | **~2 MB**       | 3:1   |
| Z07       | ~23,087 | ~240 MB          | **~30 MB**      | 8:1   |
| **Total** |         | **~247 MB**      | **~32 MB**      | —     |

**Delta vs current:** +0 MB (same file size). The Z04 and Z05 chunks use simplified geometry (only major roads, fewer vertices), so they're smaller than the full state at res 7.

**Decompressed memory at each zoom level:**

| Zoom  | Decompress                           | Memory                                |
| ----- | ------------------------------------ | ------------------------------------- |
| 5-9   | Z04 only                             | ~1 MB                                 |
| 10-12 | Z04 + Z05                            | ~7 MB                                 |
| 13+   | Z04 + Z05 + Z07 (on-demand per cell) | ~7 MB baseline + ~1 MB per cell chunk |

### All layers cost (TN)

| Layer     | Current file | Z04        | Z05        | Z07        | v3 total     | Delta       |
| --------- | ------------ | ---------- | ---------- | ---------- | ------------ | ----------- |
| Roads     | 32.6 MB      | 0.3 MB     | 2 MB       | 30 MB      | **32.3 MB**  | **-0.3 MB** |
| Business  | 53.9 MB      | 0.5 MB     | 4 MB       | 50 MB      | **54.5 MB**  | **+0.6 MB** |
| Water     | 15.4 MB      | 0.15 MB    | 1 MB       | 14 MB      | **15.2 MB**  | **-0.2 MB** |
| Buildings | 16.2 MB      | 0.2 MB     | 1.5 MB     | 15 MB      | **16.7 MB**  | **+0.5 MB** |
| **Total** | **118.1 MB** | **1.2 MB** | **8.5 MB** | **109 MB** | **118.7 MB** | **+0.6 MB** |

The delta is negligible because Z04 and Z05 use simplified geometry at coarser resolution. The total file grows by < 1%.

### Implementation

**Build pipeline change** (Rust `src/lib.rs`, writer path):

1. Group raw blocks by H3 parent resolution (res 4, res 5, res 7)
2. For each group, concatenate all raw block data into one buffer
3. Compress the buffer as a single ZSTD frame
4. Build a chunk table mapping group → (file_offset, compressed_length)
5. Rewrite the index so each entry maps cell → (chunk_id, offset_within_chunk, length)

**Reader change** (JS `index.html`):

```js
// At load: decompress Z04 chunk immediately
chunkData[0] = await zstdDecompressAsync(fileSection.Z04, dict);

// On zoom ≥ 10: decompress Z05 if not already loaded
if (zoom >= 10 && !chunkData[1])
  chunkData[1] = await zstdDecompressAsync(fileSection.Z05, dict);

// On zoom ≥ 13: decompress Z07 per viewport cell (unchanged)
```

**First-paint time comparison:**

| Zoom | Current (v2)                                    | v3 with chunks                                     |
| ---- | ----------------------------------------------- | -------------------------------------------------- |
| 8    | 786 ms download + 700 ms decompress (150 cells) | 786 ms download + **50 ms decompress** (Z04 only)  |
| 11   | 786 ms + 700 ms                                 | 786 ms + **100 ms decompress** (Z05 only)          |
| 15   | 786 ms + 700 ms                                 | 786 ms + 700 ms (Z04 warm, Z05 warm, Z07 per cell) |

---

## 2. Search indexes

### 2.1 Business name prefix index

**Problem:** `searchAll` decompresses all 18,162 business blocks to scan names. Current workaround (batch parallel decompress + cache) takes ~500ms cold, ~50ms warm.

**Proposed format:** A separate `.name_index.ptiles` file indexed by **first letter** of business name.

**File structure:**

```
TN.business_name_index.ptiles
  Index: A → chunk 0, B → chunk 1, ..., Z → chunk 25
  Chunks: 26 blocks, each containing all businesses whose name starts with that letter
  (Same PTILES binary format, just letter-keyed index instead of H3 cells)
```

**Cost:**

| Layer         | Current file | Name index  | Delta |
| ------------- | ------------ | ----------- | ----- |
| Business (TN) | 53.9 MB      | **~1.2 MB** | +2.2% |

The name index is small because it stores only what's needed for text search: `(name, lat, lon, uid, category)`. No geometry. ~12 bytes per business × 829,528 = 10 MB decompressed. ZSTD dictionary compression at 8:1 = ~1.2 MB.

**Search speed:** A search for "Starbucks" decompresses 1 chunk (letter S, ~50 KB compressed, ~0.4 MB decompressed, ~2000 businesses). That's ~30ms decompress + ~5ms scan.

**What the name index stores per record:**

```
u16  name_length
char[] name             (UTF-8)
i32  lat_micro
i32  lon_micro
u32  uid                (to cross-reference with full business file)
u8   category_id
```

**Reader:** The main page checks for a `name_index` file. If present, `searchAll` uses it instead of scanning all blocks:

```js
async function searchByName(query) {
  var firstLetter = query[0].toUpperCase();
  var chunk = nameIndexReader.getChunk(firstLetter);
  var raw = await zstdDecompressAsync(chunk, dict);
  var results = [];
  // parse records, filter by name includes, return results
  return results;
}
```

**Backward compat:** If `name_index` file is missing, fall back to full scan of business file (current behavior).

### 2.2 Business brand index

**Problem:** Searching by brand ("Starbucks", "McDonald's") still requires scanning within a letter prefix. A brand index lets you jump directly to the block for a specific brand.

**File structure:**

```
TN.business_brand_index.ptiles
  Index: hashed brand name → chunk
  Chunks: one per brand, containing all locations for that brand
```

**Cost:**

| Layer         | Current file | Brand index | Delta |
| ------------- | ------------ | ----------- | ----- |
| Business (TN) | 53.9 MB      | **~2.0 MB** | +3.7% |

The brand index stores `(brand_name, lat, lon, uid, name, address, phone, website)` for each location, grouped by brand. TN has ~50,000 unique brands. Average locations per brand: ~17. Average record size: ~40 bytes. Total: 829,528 × 40 bytes = 33 MB decompressed → ~2 MB compressed.

**Search speed:** A search for "Starbucks" decompresses 1 chunk (the brand chunk, ~50 KB compressed) and returns all 200 TN locations instantly.

**What the brand index stores per record:**

```
u16  brand_name_length
char[] brand_name     (UTF-8, stored once per chunk, not per record)
u16  name_length
char[] name
i32  lat_micro
i32  lon_micro
u32  uid
u8   category_id
u8   phone_length + char[] phone  (optional)
u8   website_length + char[] website  (optional)
```

### 2.3 Building OSM ID index

**Problem:** Looking up a building by OSM ID currently requires scanning all 7,893 building blocks. The OSM ID is stored in every record but the index is by H3 cell, not by ID.

**File structure:**

```
TN.buildings_osm_index.ptiles
  Index: OSM ID → chunk (binned by ID range, 50,000 IDs per chunk)
  Chunks: one per range, containing building records for those OSM IDs
```

**Cost:**

| Layer          | Current file | OSM ID index | Delta |
| -------------- | ------------ | ------------ | ----- |
| Buildings (TN) | 16.2 MB      | **~1.5 MB**  | +9.3% |

OSM IDs are roughly sequential. 937,709 buildings → ~19 chunks (50,000 IDs per chunk). Each chunk stores `(osm_id, lat, lon, building_type, name_ref_idx)`. Decompressed: ~12 bytes × 937,709 = 11 MB → compressed ~1.5 MB.

### 2.4 Summary table

| Index                | Purpose             | File size   | Delta vs current | Blocks | Search speed |
| -------------------- | ------------------- | ----------- | ---------------- | ------ | ------------ |
| Business name prefix | Text search on name | ~1.2 MB     | +2.2%            | 26     | ~30 ms       |
| Business brand       | Location by brand   | ~2.0 MB     | +3.7%            | ~50 K  | ~30 ms       |
| Building OSM ID      | Lookup by OSM ID    | ~1.5 MB     | +9.3%            | ~19    | ~30 ms       |
| **Total indexes**    |                     | **~4.7 MB** | **+4.0%**        |        |              |

All indexes combined add ~4.7 MB per state. For all 51 states: ~240 MB. The format is the same PTILES binary — just keyed by letter prefix, brand hash, or OSM ID range instead of H3 cell. The reader code is a generic `IndexReader` class that takes a key lookup function.

---

## 3. DDict caching in workers

**Status:** Not yet implemented.

**Problem:** Every `zstdDecompressAsync` sends the dictionary buffer to a worker and creates a fresh DDict. The DDict digest computation (~100µs in WASM) is wasted — the dict never changes per file.

**Fix:**

Worker (`decompress-worker.js`):

```js
var cachedDDict = null;
var cachedDictBytes = null;

self.onmessage = function (e) {
  var data = e.data;
  if (data.type === "init") {
    if (data.dict && data.dict.byteLength > 0) {
      cachedDictBytes = data.dict;
      cachedDDict = zstd.createDDict(data.dict);
    }
    self.postMessage({ type: "ready", id: data.id });
    return;
  }
  var dict = data.dict || cachedDictBytes;
  try {
    var dctx = zstd.createDCtx();
    var result;
    if (dict && dict.byteLength > 0) {
      result = zstd.decompressUsingDDict(dctx, data.compressed, cachedDDict);
    } else {
      result = zstd.decompress(dctx, data.compressed);
    }
    zstd.freeDCtx(dctx);
    self.postMessage({ id: data.id, result: result }, [result.buffer]);
  } catch (e) {
    self.postMessage({ id: data.id, error: e.message });
  }
};
```

Main thread (`index.html`):

```js
// Send dict once at worker init
w.postMessage({ type: "init", id: i, dict: dictBuffer }, [dictBuffer]);

// Per-call: no dict in message
w.postMessage({ id: jobId, compressed: copy }, [copy.buffer]);
```

**Impact:** Eliminates 1 DDict rebuild per decompress call. For a 512 KB dictionary, the digest computation is ~100µs in WASM. At 23,087 blocks per file, that's ~2.3s saved per full scan. For viewport rendering (35 cells), it's ~3.5ms — negligible for that case, meaningful for `searchAll`.

**No file format change.** This is pure JS optimization.

---

## 4. Already deployed

### 4.1 Lazy decompression

`loadPtilesLayer` now only downloads + parses the index. First `renderViewport` triggers per-cell decompression on-demand. First paint dropped from ~30s to ~1.2s.

### 4.2 Decoupled business from buildings

`ensureReaders()` returns only the building reader. `ensureBizReader()` is called separately when search is used. Checking the buildings checkbox no longer downloads the 54 MB business file.

### 4.3 Search result markers

Search results render as map markers (amber circles with tooltips) immediately, without closing the search panel.

### 4.4 Batch request dedup

Loading guard prevents duplicate fetches. `switchState()` clears all reader promises to free memory for the old state.

### 4.5 Center-first cell ordering

Cells are sorted by distance from map center. The 10-15 cells closest to where the user is looking render first.

### 4.6 Worker count capped at 4

Limited ZSTD worker count to 4 to avoid WASM memory exhaustion on high-core-count machines.

### 4.7 WASM memory fallback

All WASM decode functions fall back to JS decoders if WASM init fails.

### 4.8 DDict caching in workers

ZSTD dictionary is sent to each worker once at init via `set_dict` message. Workers create the DDigested dictionary (`createDDict`) once and reuse it for all subsequent `decompressUsingDDict` calls. Eliminates ~23K dict digest rebuilds per state load. Per-call postMessage no longer sends the dict buffer, saving ~512KB of structured clone copy per decompress.

## 5. Planned optimizations (not yet implemented)

### 5.1 Road class LOD filtering

Filter road segments by class based on zoom level — only render roads that are visible at the current scale.

| Zoom  | Classes rendered           |
| ----- | -------------------------- |
| 5-8   | Highways only (class 0-2)  |
| 9-10  | Highways + arterials (0-5) |
| 11-12 | + Collectors (0-7)         |
| 13    | + Residential (0-10)       |
| 14+   | All classes (0-15)         |

Implementation: one-line check in the render loop before adding to the SVG path batch. At zoom 8, 90% of road segments are skipped. Combined with batched SVG rendering, this reduces SVG path count from 3000+ to ~50 at low zoom.

### 5.2 Batched SVG path rendering

Replace individual `L.polyline`/`L.polygon` calls with building one `d` attribute string per style class. Inject as a single `<path>` element. 16 road classes × 1 `<path>` each = 16 SVG nodes instead of 3000. Same for H3 grid (all hexagons share one style).

Implementation plan:

- Collect features by style class during the render loop
- After each batch, build a `d` string for each class: `"M x1 y1 L x2 y2 ... M ..."`
- Set the `d` attribute on the pre-created `<path>` element
- No Leaflet feature objects, no per-feature event handlers

Expected savings: ~200ms of Leaflet object allocation + SVG DOM insertion per viewport render.

### 5.3 Viewport culling

Before adding a feature, check if its bounding box intersects the map bounds. Features at the edge of a viewport cell that extend off-screen are skipped. At zoom 10-12, res 7 cells cover a wide area — 20-30% of features are off-screen.

### 5.4 Zoom-band chunk merging (v3 format)

See section 1 above. The format change that makes everything faster at low zoom by compressing blocks into zoom-band chunks (Z04, Z05, Z07).

## 6. Implementation order

| Priority | Change                    | Effort  | Impact                           | Format change |
| -------- | ------------------------- | ------- | -------------------------------- | ------------- |
| 0        | **DDict caching**         | DONE    | 3-5× searchAll speed             | None          |
| 0        | **Center-first cells**    | DONE    | Faster first paint               | None          |
| 0        | **Lazy decompress**       | DONE    | 30s → 1.2s first paint           | None          |
| 0        | **Decoupled readers**     | DONE    | No spurious downloads            | None          |
| 1        | **Road class LOD**        | 2 hours | 10× fewer features at low zoom   | None          |
| 2        | **Batched SVG paths**     | 4 hours | Eliminates 3000+ Leaflet objects | None          |
| 3        | **Viewport culling**      | 1 hour  | 20-30% fewer features rendered   | None          |
| 4        | **Zoom-band chunks**      | 1 week  | 16× less data at low zoom        | v3            |
| 5        | **Business name index**   | 3 days  | Search in 30ms vs 500ms          | New file      |
| 6        | **Business brand index**  | 1 day   | Brand search in 30ms             | New file      |
| 7        | **Building OSM ID index** | 1 day   | OSM ID lookup in 30ms            | New file      |

**Total storage increase for TN with all indexes:** ~4.7 MB (+4.0%).
**Total storage increase for 51 states with all indexes:** ~240 MB (vs ~6 GB for all PTILES files).
