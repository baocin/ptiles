# Build scripts

One builder per layer. If a layer is not in the table below, nothing here
currently builds it.

Every `.ptiles` file opens with 7 ASCII magic bytes identifying the layer, then
a version byte scoped to that magic — there is no repo-wide version. The table
records what the **shipped** files actually contain, read back from
`v4-20260711`, not what any script claims.

## Current builders

| Layer | Output | Magic | Ver | Builder | Source |
| --- | --- | --- | --- | --- | --- |
| Buildings | `{ST}.buildings_v9.ptiles` | `PTILESF` | 9 | `build_state_v8.py` | per-state OSM PBF |
| Business / POI | `{ST}.business_v4.ptiles` | `PTILESB` | 4 | `build_full_ptilesb.py` | Overture + Foursquare parquet |
| Business name index | `{ST}.business_name_index.ptiles` | `PTILESX` | 1 | `build_business_name_index.py` | the `business_v4` file above |
| Highways | `{ST}.highways_v2.ptiles` | `PTILESR` | 2 | `build_us_highways.py` | per-state OSM PBF |
| Roads | `roads/{ST}.roads.ptiles` | `PTILESR` | 2 | `build_roads.py` → `upgrade_roads_v2.py` | per-state OSM PBF |
| Water | `{ST}.water_v1.ptiles` | `PTILESW` | 1 | `build_water.py` | per-state OSM PBF |
| Places | `{ST}.places_v1.ptiles` | `PTILESP` | 1 | `build_places.py` | per-state OSM PBF |
| Parks | `{ST}.parks_v1.ptiles` | `PTILESN` | 1 | `build_parks.py` | per-state OSM PBF |
| Rail | `{ST}.rail_v1.ptiles` | `PTILEST` | 1 | `build_rail.py` | per-state OSM PBF |
| Address | `{ST}.address_v2.ptiles` ‡ | `PTILESD` † | 2 | `build_address.py` | per-state OSM PBF |
| Admin | `US.admin.ptiles` | `PTILESA` | 1 | `build_admin.py` | Census shapefiles |
| Cameras | `US.camera.ptiles` | `PTILESC` | 1 | `build_points.py --layer camera` | per-state OSM PBF |
| Signals | `US.signals.ptiles` | `PTILESS` | 1 | `build_points.py --layer signals` | per-state OSM PBF |

Three entries need explaining:

**‡ Address is now v2, and the table above no longer matches `v4-20260711`.**
Everything else in this table still describes the shipped files. Address does
not: `build_address.py` is `VERSION = 2` since the magic fix below, so it now
emits `{ST}.address_v2.ptiles` where the published set holds `address_v1`.

Filenames carry the version because each builder derives the suffix from its own
`VERSION` constant rather than hardcoding it — the name cannot drift from the
version byte in the header. Layers with no version in the published name
(`roads`, `business_name_index`, `admin`, `camera`, `signals`) are left alone.


**Roads is a two-step.** `build_roads.py` writes v1, then `upgrade_roads_v2.py`
rewrites it to the v2 index (37/38-byte entries with a per-cell bbox). Running
only the first leaves you with files the current readers treat as v1.

**† Address magic was wrong, and published files still carry it.**
`build_address.py` used to set `MAGIC = b"PTILESA2\x00"`; `write_header` packs
`magic[:7]`, so the trailing `2` was dropped and address shipped as `PTILESA` —
the admin magic. The builder now stamps `PTILESD` per `SPEC.md`, and
`write_header` raises rather than truncating, in both `shared.py` and
`ptiles/codec.py`.

Every address file published up to and including `v4-20260711` still carries
`PTILESA` and is indistinguishable from an admin file by magic byte. They are
only corrected by a rebuild and republish. Until then a reader dispatching on
magic must either accept `PTILESA` for address — and disambiguate some other
way, e.g. the filename — or refuse pre-fix files outright. `python -m ptiles
inspect` reports `PTILESA` as "admin (or a pre-fix address file)" for this
reason.

## Shared modules

| File | What |
| --- | --- |
| `shared.py` | header/index writers, block encoding, zstd dictionary training |
| `encoding.py` | varint, zigzag, string and coordinate primitives |
| `encode_v8.py` | v8/v9 building record encode/decode, height parsing |
| `states.py` | state list, abbreviations, bounding boxes |
| `pmtiles_reader.py` | PMTiles source reader |

Builders resolve these with `sys.path.insert(dirname(__file__))` and a bare
`from shared import ...`, so they only work from this directory. That is why
everything is flat rather than sorted into subdirectories.

## Tools

| File | What |
| --- | --- |
| `read_admin.py`, `read_roads.py`, `read_water.py` | inspect a built file |
| `deploy_roads.py`, `upload_v2.sh` | publish to the tile host |
| `download_pbfs.sh` | fetch per-state OSM extracts |
| `ptiles_status.sh`, `watch_completion.py` | monitor a long batch build |

For reading files generally, prefer the Python library: `python -m ptiles
inspect FILE`, and the readers in `ptiles/`.

## archive/

Superseded and one-off scripts, kept for reference only. **Nothing here is
maintained and none of it produces a current format.** Their imports assume the
parent directory, so they will not run from where they now sit.

It holds the parquet pipeline (`build_national_parquet.py` and friends),
one-shot repairs and migrations (`rebuild_v2.py`, `split_business_v2.py`,
`merge_us_poi.py`), batch wrappers (`launch_all_states.sh`, `run_us_build.sh`),
and per-layer "build all" loops superseded by the `--all` flag on the current
builders.

Deleted outright rather than archived, because they only ever emitted formats
no longer published: nine buildings builders stuck at v8 (`build_us_v8.py`,
`extract_buildings.py`, `generate_state_v8.py`, …) and four business builders
at v1/v3 (`build_business.py`, `build_us_business.py`, `build_us_poi*.py`).
They are in git history if ever needed.
