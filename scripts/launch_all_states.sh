#!/bin/bash
# Launch full PTILES v8 generation for every US state + DC.
# Starts the pmtiles HTTP server, then runs the Python generator in background.

set -euo pipefail

PMTILES_BIN="${PMTILES_BIN:-/tmp/pmtiles}"
PMTILES_FILE="${PMTILES_FILE:-/home/aoi/data/protomaps/20260513.pmtiles}"
PORT=8099
LOG_DIR="/home/aoi/kino/projects/ptiles/logs"
mkdir -p "$LOG_DIR"

if [ ! -x "$PMTILES_BIN" ]; then
    echo "Downloading go-pmtiles binary..."
    curl -sL "https://github.com/protomaps/go-pmtiles/releases/download/v1.30.2/go-pmtiles_1.30.2_Linux_x86_64.tar.gz" | tar -xz -C /tmp
    chmod +x "$PMTILES_BIN"
fi

echo "Starting pmtiles serve on :$PORT ..."
"$PMTILES_BIN" serve "$PMTILES_FILE" --port "$PORT" > "$LOG_DIR/pmtiles_serve.log" 2>&1 &
SERVE_PID=$!
echo "pmtiles serve PID: $SERVE_PID"

sleep 3
if ! curl -sf "http://localhost:$PORT/0/0/0" -o /dev/null; then
    echo "ERROR: pmtiles server did not become ready"
    kill $SERVE_PID 2>/dev/null || true
    exit 1
fi
echo "Server ready."

cd /home/aoi/kino/projects/ptiles

echo "Launching state generator (all 51)..."
nohup uv run --with h3 --with mapbox-vector-tile --with zstandard \
    python scripts/generate_state_v8.py --all \
    > "$LOG_DIR/generate_all_states.log" 2>&1 &

GEN_PID=$!
echo "Generator PID: $GEN_PID"
echo "Logs: $LOG_DIR/generate_all_states.log"
echo "Monitor with: tail -f $LOG_DIR/generate_all_states.log"

# Optional: wait for completion and shut down server
# wait $GEN_PID
# kill $SERVE_PID 2>/dev/null || true
# echo "Generation complete."