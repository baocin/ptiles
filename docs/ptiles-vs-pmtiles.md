# PTILES vs PMTiles

Two different tools for two different problems.

## PMTiles

**What it is:** A single-file archive of pre-rendered Mapbox Vector Tiles (MVT), organized by z/x/y tile coordinate. Designed for cheap self-hosted map rendering.

**How it works:**

- Client requests a tile at `{z}/{x}/{y}.mvt`
- Server (or HTTP range request against static hosting) returns the MVT blob for that tile
- Client decodes the protobuf and renders it on a map canvas

**Bandwidth per query:** ~50-200 KB (one z14 tile covers ~600m x 600m, contains hundreds of features)

**Use case:** Slippy map rendering. "Draw this 256x256 tile."

**Source:** Overture Maps building footprints at zoom 14, encoded as MVT protobuf.

## PTILES

**What it is:** A compact binary format for geospatial feature lookup. Each file covers one layer (buildings, roads, water, business) for a geographic region.

**How it works:**

- Hash GPS point to H3 cell (constant time)
- Look up cell in spatial index (hash map, O(1))
- Decompress one zstd block (~1-5 KB)
- Iterate buildings in that cell only

**Bandwidth per query:** ~1-5 KB (one H3 res 7 cell covers ~5km x 5km median)

**Use case:** Spatial feature lookup. "What building am I in? What road is nearest?"

**Offline:** Fully readable with no server. Open the file, read the header, seek to the index, seek to the block, decompress.

## Key differences

|                     | PTILES                             | PMTiles                                           |
| ------------------- | ---------------------------------- | ------------------------------------------------- |
| Purpose             | Feature lookup at a point          | Render map tiles                                  |
| Spatial index       | H3 cell (hash lookup, O(1))        | Tile coordinate (z/x/y)                           |
| Query at lat/lon    | One block, iterate cell's features | Fetch z14 tile, decode protobuf                   |
| Bandwidth per query | ~1-5 KB                            | ~50-200 KB                                        |
| Offline-friendly    | Yes — one file, no server          | Needs a PMTiles server or HTTP range support      |
| Server requirement  | None (read the file directly)      | HTTP range requests or pmtiles server             |
| Mobile-friendly     | Yes — single file, index-first     | Yes with tile caching, but needs server roundtrip |

## Why both exist

The 130GB PMTiles file on R2 (`20251024.pmtiles`) was the source data for building the PTILES files. It's Overture Maps building footprints at zoom 14, served as MVT. You could serve it with `pmtiles serve` and build a slippy map. But every GPS lookup would fetch a ~100KB+ tile and decode hundreds of features.

The PTILES files extract the same data, re-index it by H3 cell, and compress it with a trained zstd dictionary. A GPS lookup costs ~1-5KB and returns only the relevant buildings. The 51 per-state files total ~1.1 GB vs the source PMTiles at 130 GB.

**PMTiles answers "draw this tile." PTILES answers "what's at this coordinate."**
