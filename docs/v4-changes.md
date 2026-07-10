# PTiles v4 — Changes for Next Rebuild

Stack of format + builder changes queued for the next full 51-state rebuild.

## Business (PTILESB) — v4

**Format changes (SPEC.md § Business v4):**

- `flags` byte: 0x10 = amenities block, 0x20 = operating_status (2 bits), 0x40 = unused, 0x80 = chain_count
- Amenities block: drive_through, wheelchair, internet (zero-cost booleans), smoking, capacity, amenity_type, amenity_extra
- `ext_flags` u16: star_rating (0x10), opening_hours (0x20), source_version (0x40), updated (0x80)
- Opening hours block: day_bitmask + minute ranges, 5 bytes/range
- Sidecar: `amenities.json` (global amenity type index)

**Optimizations (builder only, no format change):**

- Drop `record_len` u32 prefix — sequential parse by `feature_count`
- Sequential uids instead of sha256 hash — varint delta from ~5 bytes → 1 byte
- i16 cell-relative coords instead of i32 absolute — saves 4 bytes/record
- Per-block string table for business names (same as buildings v8) — 1-byte table ref instead of full u16_str

**Space:** 54 MB → ~45 MB (TN). US-wide: ~975 MB → ~810 MB.

## Buildings (PTILESF) — v9

**Format changes (SPEC.md § Buildings v9):**

- `flags2` byte: 0x20 = business_tag (shop or amenity value), 0x40 = opening_hours string, 0x80 = reserved
- `business_tag`: u8 table ref into block-local string table. Values: `shop=*` or `amenity=*` from OSM PBF tags.
- `opening_hours`: u8-length-prefixed raw OSM opening_hours string. Parsed at query time by `is_open_now()`.
- Builder reads `shop`, `amenity`, `opening_hours` tags from OSM PBF ways (already on the same building objects).

**Runtime:**

- `Building { business_tag, opening_hours }` — nil for ~85% of buildings
- `is_open_now(osm_hours, tz_offset) -> bool` — parse OSM string, return open/closed

**Space:** 1.14 GB → ~1.17 GB (~3% increase).

## Highways — all 51 states

- `build_us_highways.py` (renamed from `build_missing_highways.py`) now builds all 51 states
- Same format as roads (PTILESR magic), filtered to motorway/trunk/primary + links
- Router reads it as a regular roads file for long-distance corridor routing
- US-wide: ~50-100 MB

## Name index (PTILESX) — v4 builder compat

- `build_business_name_index.py` decodes v4 business files (sequential uid, i16 coords, no record_len)
- Output format unchanged (v1)

## Decoder changes

### Rust (`timeline/ptiles/src/`)

- `business.rs`: `parse_block_v4`, `parse_record_v4`, version dispatch in `decompress_block`
- `business.rs`: skip empty cells in `nearby()` (feature_count == 0 guard)
- `business.rs`: `parse_attrs` returns `(Business, usize)`
- `buildings.rs`: `Building { business_tag, opening_hours }` fields
- `buildings.rs`: `is_open_now()` runtime function
- All existing tests pass (7 business + 4 buildings)

### Python (`ptiles/scripts/`)

- `build_full_ptilesb.py`: VERSION=4, `encode_v4()`, sequential uid, i16 coords, no record_len
- `encode_v8.py`: VERSION bumped, `shop`/`amenity`/`opening_hours` in encoder + string table
- `extract_buildings.py`: read `shop`, `amenity`, `opening_hours` from OSM PBF tag loop
- `build_business_name_index.py`: `decode_business_record_v4()` for v4 input files

## Rebuild command

```bash
# All 51 states, sequential (parallel by state is fine, each state is independent)
cd ~/kino/projects/ptiles/scripts

# Buildings v9
python extract_buildings.py --all

# Business v4
python build_full_ptilesb.py

# Highways (all states)
python build_us_highways.py

# Name index (one per state)
for s in AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY; do
    python build_business_name_index.py $s
done
```

Exit: v4 business files overwrite v3. v9 buildings overwrite v8. Highways land in `{STATE}.highways.ptiles`. Name index overwrites in-place. Old files preserved in git before commit.
