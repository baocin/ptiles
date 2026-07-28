# Brand Favicons — Download & Upload

Business logos for ptiles brand/business layer. Two sources, same pipeline.

## Data flow

```
parquet/v2/*/business_v2.parquet  (50 states, website column)
  → extract registered domain (second-level + TLD)
  → md5 hash → filename
  → download from Google s2/favicons + DDG ip3
  → copy to external drive
  → upload to R2
```

## Script

`scripts/download_brand_favicons.py` — accepts `--source google|ddg|both` (default: both), `--workers N` (default: 25), `--resume` (default: on).

Runs per-state: reads the parquet, collects unique registered domains not yet in the output dir, fires a ThreadPoolExecutor batch. Progress heartbeat every 10s to `~/.favicon-{source}-progress.txt`. Resume writes last-completed state to `~/.favicon-state-order.txt`.

## Sources

| Source | URL                           | Output   | Ext    | Rate  |
| ------ | ----------------------------- | -------- | ------ | ----- |
| Google | `s2/favicons?domain={}&sz=64` | 64px PNG | `.png` | ~42/s |
| DDG    | `ip3/{}.ico`                  | ICO      | `.ico` | ~24/s |

Google is ~75% faster and produces PNGs directly (no ICO conversion needed). DDG finds icons Google misses on long-tail domains. Union both on upload.

## Output dirs (NFS, 100.94.73.109:/mnt/tmp/core)

| Dir                                           | Files | Size   | Status             |
| --------------------------------------------- | ----- | ------ | ------------------ |
| `/mnt/core/kino/ingest/incoming-favicons`     | 6.4M  | ~6GB   | Google, complete   |
| `/mnt/core/kino/ingest/incoming-favicons-ddg` | 2.8M  | ~1.4GB | DDG, still running |

## Filename scheme

`md5(domain).png` / `md5(domain).ico` — md5 avoids filesystem issues with dots/slashes in domains (starbucks.com → `d1f5a9b8...png`). Mapping back to domain is on the consumer side (brand ptiles references by domain, viewer resolves to md5 URL).

## Upload

1. Copy from NFS to external drive while DDG finishes:
   ```
   rsync -ah --progress /mnt/core/kino/ingest/incoming-favicons/ /run/media/aoi/haze/ptiles-brand-favicons/
   ```
2. After DDG finishes, dedupe: for each domain in the Google set, if DDG has a larger file, replace. Google wins ties.
3. Upload to R2:
   ```
   rclone sync /run/media/aoi/haze/ptiles-brand-favicons/ :s3:steele.red/brand-favicons/ \
     --s3-env-auth --s3-region auto
   ```
4. Served from `https://maps.mydatatimeline.com/brand-favicons/{md5}.png`

## R2 naming note

Old bucket (`maps.mydatatimeline.com/brand-favicons/{domain}.png`, ~23K icons) uses bare domain names as filenames. New batch uses md5. Both served from same subdomain path — no collision since md5 hex strings don't overlap with bare domain names.

## Covered rows

Top 23K domains (old R2 set): 4.5% of OSM rows with websites.
6.4M new domains: long tail. ~12.5M unique registered domains total across 50 states.
