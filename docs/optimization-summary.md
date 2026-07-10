# PTILES Performance — Final Summary

## Implemented (no format change)

| Optimization                                                                                    | Impact                                                   | Lines changed                                 |
| ----------------------------------------------------------------------------------------------- | -------------------------------------------------------- | --------------------------------------------- |
| **Lazy decompress** — download + parse index, don't decompress blocks until viewport render     | 30s → 1.2s first paint                                   | `loadPtilesLayer()`                           |
| **DDict caching** — dictionary sent once to workers at init, cached DDict reused for all blocks | 3-5× searchAll speed                                     | `decompress-worker.js`, `setDictOnWorkers()`  |
| **Decoupled business from buildings** — buildings checkbox doesn't download 54MB business file  | No spurious downloads                                    | `ensureReaders()` / `ensureBizReader()` split |
| **Center-first cell ordering** — cells sorted by distance from map center                       | ~20% faster perceived load                               | `renderPtilesForCells()`                      |
| **Zoom range fix** — LOD resolution at zoom < 10, res 7 for features at zoom ≥ 10               | H3 grid renders at correct density, features match index | `renderViewport()` queryRes                   |
| **Worker count capped at 4** — prevents WASM OOM on high-core machines                          | No crashing                                              | `initZstdWorkers()`                           |
| **WASM fallback to JS** — if WASM init fails, JS decoders take over                             | Graceful degradation                                     | All decode wrappers                           |
| **Buffer detachment fix** — dict copied before transfer to workers, non-transferrable send      | Fixes corrupt file index                                 | `setDictOnWorkers()`                          |
| **Road class LOD** — filter by road type (highway/arterial/residential) per zoom                | 90% fewer features at zoom 8                             | `roadClassVisible()`                          |
| **Batched SVG paths** — one `<path>` element per style class instead of 3000+                   | 49 SVG paths vs 3254                                     | Full render loop rewrite                      |
| **Viewport culling** — skip features outside map bounds at zoom < 12                            | 20-30% fewer features                                    | `cullCheck` in render loop                    |
| **H3 grid direct SVG** — write hexagons directly to SVG instead of Leaflet polygons             | No Leaflet per-feature overhead                          | `buildPolygonD()`                             |
| **Business search markers** — all results render as map circles immediately                     | Search UX improvement                                    | `searchMarkers` layer group                   |
| **Search sidebar stays open** — clicking a result doesn't close the panel                       | UX improvement                                           | `doSearch()`                                  |
| **Business parallel decompress + cache** — `searchAll` decompresses in parallel batches         | ~500ms cold → ~50ms warm                                 | `BusinessReader.searchAll()`                  |

## Requires format change

| Optimization                                                                                      | Format             | Effort         | Impact                                                      |
| ------------------------------------------------------------------------------------------------- | ------------------ | -------------- | ----------------------------------------------------------- |
| **Zoom-band chunks** (v3) — Z04/Z05/Z07 frames, decompress one frame per zoom instead of per cell | New header version | 1 week builder | 16× less data at low zoom, 50ms decompress instead of 700ms |
| **Business name prefix index** — `TN.business_name_index.ptiles`, 26 chunks by first letter       | New file           | 3 days         | Search in 30ms vs 500ms                                     |
| **Business brand index** — `TN.business_brand_index.ptiles`, one chunk per brand                  | New file           | 1 day          | Brand search in 30ms                                        |
| **Building OSM ID index** — `TN.buildings_osm_index.ptiles`, indexed by OSM ID range              | New file           | 1 day          | OSM ID lookup in 30ms                                       |

## Without v3 format change: what the user experiences today

- **Zoom 4-9**: H3 grid only, no features. Status: "H3 res 4 — zoom in for details"
- **Zoom 10**: Roads filtered to highways/arterials only, 16 SVG paths. ~100ms decompress + render
- **Zoom 13**: All road classes, water, H3 grid. ~700ms decompress + render. 49 SVG paths
- **Search**: Name search decompresses all 18K business blocks in parallel. ~500ms cold, ~50ms warm
- **Panning**: New viewport decompresses ~35 cells. Block cache prevents re-decompression on revisit
- **Memory**: ~96MB decompressed (roads + water). Only one state's data at a time
