#!/usr/bin/env bash
# Fetch the bulk address corpora OSM cannot supply.
#
# OSM has ~154k addr:housenumber objects for all of Tennessee, against ~3M
# housing units, so the address layer answers "no address near here" almost
# everywhere. These two sources are what a real address layer would be built
# from:
#
#   OpenAddresses  4 regional collections, ~5.5 GB zipped, CSV per source
#   NAD r23        USDOT National Address Database, 7.6 GB zipped, ~80M records
#
# Resumable (`curl -C -`), so an interrupted run costs nothing: re-run it.
set -uo pipefail

DEST="${DEST:-/mnt/core/timeline-ptiles-cache/addresses}"
mkdir -p "$DEST/openaddresses" "$DEST/nad"

fetch() { # url dest_path
  echo "==> $(date -u +%H:%M:%S) $2"
  curl -fL -C - --retry 5 --retry-delay 10 --retry-all-errors \
    -o "$2" "$1" || echo "!!! FAILED $2"
}

for region in us_northeast us_midwest us_south us_west; do
  fetch "https://data.openaddresses.io/openaddr-collected-$region.zip" \
    "$DEST/openaddresses/openaddr-collected-$region.zip"
done

# The Socrata blob id changes with each quarterly release; r23 (2026-06-30) is
# what `api/views/fc2s-wawr.json` pointed at when this was written.
fetch "https://data.transportation.gov/api/views/fc2s-wawr/files/b189f78b-2262-44e8-b3b6-5c4094c12da5?filename=TXT.zip" \
  "$DEST/nad/NAD_r23_TXT.zip"

echo "==> done"
ls -la "$DEST/openaddresses" "$DEST/nad"
