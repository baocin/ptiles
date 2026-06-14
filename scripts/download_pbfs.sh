#!/bin/bash
# Download all US state OSM PBFs from Geofabrik
# ~11 GB total, ~20 min at 8 MB/s
set -euo pipefail

DIR="/home/aoi/kino/projects/ptiles/data/pbfs"
mkdir -p "$DIR"
LOG="$DIR/download.log"

STATES=(
  "alabama" "alaska" "arizona" "arkansas" "california" "colorado"
  "connecticut" "delaware" "district-of-columbia" "florida" "georgia"
  "hawaii" "idaho" "illinois" "indiana" "iowa" "kansas" "kentucky"
  "louisiana" "maine" "maryland" "massachusetts" "michigan" "minnesota"
  "mississippi" "missouri" "montana" "nebraska" "nevada" "new-hampshire"
  "new-jersey" "new-mexico" "new-york" "north-carolina" "north-dakota"
  "ohio" "oklahoma" "oregon" "pennsylvania" "rhode-island"
  "south-carolina" "south-dakota" "tennessee" "texas" "utah" "vermont"
  "virginia" "washington" "west-virginia" "wisconsin" "wyoming"
)

total=0
done=0
for st in "${STATES[@]}"; do
  fname="${st}-latest.osm.pbf"
  if [ -f "$DIR/$fname" ] && [ -s "$DIR/$fname" ]; then
    sz=$(stat -c%s "$DIR/$fname" 2>/dev/null)
    echo "$(date) SKIP $st ($((sz/1024/1024))MB already exists)" | tee -a "$LOG"
    total=$((total + sz))
    done=$((done + 1))
    continue
  fi
  echo "$(date) START $st" | tee -a "$LOG"
  curl -sL "https://download.geofabrik.de/north-america/us/$fname" -o "$DIR/$fname"
  if [ -f "$DIR/$fname" ] && [ -s "$DIR/$fname" ]; then
    sz=$(stat -c%s "$DIR/$fname")
    echo "$(date) DONE  $st ($((sz/1024/1024))MB, $((sz/1024/1024)) MB)" | tee -a "$LOG"
    total=$((total + sz))
    done=$((done + 1))
  else
    echo "$(date) FAIL  $st" | tee -a "$LOG"
  fi
done

echo "=== Complete: $done states, $((total/1024/1024/1024)) GB ===" | tee -a "$LOG"
ls -lh "$DIR/" | tee -a "$LOG"
