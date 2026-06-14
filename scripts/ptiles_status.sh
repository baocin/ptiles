#!/bin/bash
# Quick status for PTILES per-state v8 generation
set -euo pipefail

DATA_DIR="/home/aoi/kino/projects/ptiles/data/states"
LOG_FILE="/home/aoi/kino/projects/ptiles/logs/generate_all_states.log"

echo "=== PTILES v8 State Generation Status ==="
echo "Time: $(date)"
echo

if [ -d "$DATA_DIR" ]; then
    count=$(ls "$DATA_DIR"/*.buildings_v8.ptiles 2>/dev/null | wc -l)
    echo "States completed: $count / 51"
    echo
    echo "Latest 8 files:"
    ls -lh "$DATA_DIR"/*.buildings_v8.ptiles 2>/dev/null | tail -8
else
    echo "Data directory not yet created (generator may still be initializing)"
fi

echo
if [ -f "$LOG_FILE" ]; then
    echo "=== Last 5 lines of generator log ==="
    tail -n 5 "$LOG_FILE"
else
    echo "Log file not found yet"
fi

echo
echo "To watch live: tail -f $LOG_FILE"
echo "To check again: ~/kino/projects/ptiles/scripts/ptiles_status.sh"