#!/usr/bin/env python3
"""Watch for completion of all 51 state PTILES files and notify."""
import time
from pathlib import Path
from datetime import datetime

TARGET = 51
DATA_DIR = Path("/home/aoi/kino/projects/ptiles/data/states")
LOG = Path("/home/aoi/kino/projects/ptiles/logs/generate_all_states.log")

def count_files():
    if not DATA_DIR.exists():
        return 0
    return len(list(DATA_DIR.glob("*.buildings_v8.ptiles")))

print(f"[{datetime.now().isoformat()}] Watching for PTILES state completion ({TARGET} states)...")

last_count = 0
while True:
    c = count_files()
    if c != last_count:
        print(f"[{datetime.now().isoformat()}] Progress: {c}/{TARGET}")
        last_count = c
    if c >= TARGET:
        print(f"[{datetime.now().isoformat()}] ALL {TARGET} states complete!")
        # Optional: send a desktop notification or write a done marker
        (DATA_DIR / ".generation_complete").write_text(datetime.now().isoformat())
        break
    time.sleep(60)  # check every minute

# Also tail the log one last time for summary
if LOG.exists():
    print("=== Final log lines ===")
    print(LOG.read_text().splitlines()[-10:])
