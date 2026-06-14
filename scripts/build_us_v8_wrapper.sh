#!/bin/bash
# US PTILES v8 Build — PMTiles → Buildings
# Spawned by cron, survives terminal death
set -euo pipefail

PMTILES_BIN=/tmp/pmtiles
PMTILES_SRC=/home/aoi/data/protomaps/20260513.pmtiles
LOG=/tmp/us_v8_build.log
PIDFILE=/tmp/pmtiles_serve.pid

echo "=== US v8 Build Started $(date) ===" | tee -a "$LOG"

# Start pmtiles serve in background
echo "Starting pmtiles serve..." | tee -a "$LOG"
$PMTILES_BIN serve "$PMTILES_SRC" --port 8099 2>&1 | tee -a "$LOG" &
SERVE_PID=$!
echo $SERVE_PID > "$PIDFILE"
sleep 2

# Verify server is up
if ! kill -0 $SERVE_PID 2>/dev/null; then
    echo "FATAL: pmtiles serve failed to start" | tee -a "$LOG"
    exit 1
fi
echo "pmtiles serve running (PID $SERVE_PID)" | tee -a "$LOG"

# Run extraction
echo "Starting extraction..." | tee -a "$LOG"
cd /home/aoi/kino/projects/ptiles/scripts
uv run --with pmtiles --with mapbox_vector_tile --with h3 --with zstandard --with shapely \
    python3 /home/aoi/kino/projects/ptiles/scripts/build_us_v8_http.py \
    2>&1 | tee -a "$LOG"

RC=${PIPESTATUS[0]}
echo "Extraction exit code: $RC" | tee -a "$LOG"

# Cleanup
kill $SERVE_PID 2>/dev/null || true
rm -f "$PIDFILE"
rm -rf /tmp/ptiles_us_v8/

echo "=== US v8 Build Finished $(date) ===" | tee -a "$LOG"
exit $RC
