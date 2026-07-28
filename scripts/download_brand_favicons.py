#!/usr/bin/env python3
"""Download favicons from Google + DDG in parallel, state by state.

Usage: python3 download_favicons.py [--source google|ddg|both] [--workers 20] [--resume]

Consolidates the old google-only + ddg-only scripts into one with:
- Multistream: each state reads its parquet, splits the batch across N inner workers
- Heartbeat: writes progress every 10s regardless of completion % (not just every CHECK_INTERVAL)
- Resume: reads existing files on disk, skips already-downloaded
- Clean termination: no rm -rf, no upload code embedded

Run with --source both to launch Google+DDG as subprocesses (independent state iterators).
Run with --source google or --source ddg for single-source.
"""

import argparse
import hashlib
import os
import sys
import time
import concurrent.futures
from urllib.request import Request, urlopen

SOURCE_CONFIG = {
    "google": {
        "out": "/mnt/core/kino/ingest/incoming-favicons",
        "ext": ".png",
        "url_fmt": "https://www.google.com/s2/favicons?domain={}&sz=64",
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "log": "/home/aoi/.favicon-progress.txt",
    },
    "ddg": {
        "out": "/mnt/core/kino/ingest/incoming-favicons-ddg",
        "ext": ".ico",
        "url_fmt": "https://icons.duckduckgo.com/ip3/{}.ico",
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "log": "/home/aoi/.favicon-ddg-progress.txt",
    },
}

PARQUET_DIR = "/mnt/core/kino/ptiles/data/parquet/v2"
MIN_SIZE = 100
TIMEOUT = 8
HEARTBEAT = 10  # progress flush interval in seconds
STATE_ORDER_FILE = (
    "/home/aoi/.favicon-state-order.txt"  # resume: which state we were on
)


def web2domain(w):
    if not w or not w.strip():
        return None
    w = w.strip().lower()
    if "://" in w:
        w = w.split("://")[1]
    w = w.rstrip("/").split("?")[0].split("#")[0]
    if w.startswith("www."):
        w = w[4:]
    parts = w.split(".")
    if len(parts) < 2:
        return None
    tld = parts[-1]
    if len(tld) <= 3 and len(parts) >= 2:
        return f"{parts[-2]}.{parts[-1]}"
    elif len(parts) >= 3:
        return f"{parts[-3]}.{parts[-2]}.{parts[-1]}"
    return None


def worker(source, domain):
    """Download one favicon. Returns 'ok', 'small', 'fail'."""
    url = source["url_fmt"].format(domain)
    try:
        req = Request(url, headers={"User-Agent": source["ua"]})
        resp = urlopen(req, timeout=TIMEOUT)
        data = resp.read()
        if len(data) >= MIN_SIZE:
            fn = hashlib.md5(domain.encode()).hexdigest() + source["ext"]
            with open(os.path.join(source["out"], fn), "wb") as f:
                f.write(data)
            return "ok"
        return "small"
    except Exception:
        return "fail"


def run_source(name, source, workers, resume):
    import pyarrow.parquet as pq

    os.makedirs(source["out"], exist_ok=True)

    # Read all 2-letter dirs = states
    try:
        all_states = sorted(d for d in os.listdir(PARQUET_DIR) if len(d) == 2)
    except FileNotFoundError:
        print(
            f"[{name}] FATAL: parquet dir {PARQUET_DIR} not found. Is NFS mounted?",
            flush=True,
        )
        sys.exit(1)

    if not all_states:
        print(f"[{name}] No 2-letter state dirs found in {PARQUET_DIR}", flush=True)
        return

    # Determine iteration order and resume point
    state_order = list(reversed(all_states)) if name == "ddg" else all_states
    start_idx = 0

    if resume and os.path.exists(STATE_ORDER_FILE.format(source=name)):
        try:
            with open(STATE_ORDER_FILE, "r") as f:
                last_state = f.read().strip()
            if last_state in state_order:
                start_idx = state_order.index(last_state) + 1
                print(
                    f"[{name}] Resuming from {last_state} (index {start_idx}/{len(state_order)})",
                    flush=True,
                )
        except Exception:
            pass

    total_ok = 0
    total_small = 0
    total_fail = 0
    t0 = time.time()
    last_heartbeat = t0

    for sidx in range(start_idx, len(state_order)):
        s = state_order[sidx]
        f = os.path.join(PARQUET_DIR, s, "business_v2.parquet")
        if not os.path.exists(f):
            print(f"[{name}] SKIP {s}: no parquet", flush=True)
            continue

        # Read domains for this state
        t = pq.read_table(f, columns=["website"])
        batch = []
        for w in t.column("website").to_pylist():
            d = web2domain(w)
            if d is None:
                continue
            fn = os.path.join(
                source["out"], hashlib.md5(d.encode()).hexdigest() + source["ext"]
            )
            if not os.path.exists(fn) or os.path.getsize(fn) < MIN_SIZE:
                batch.append(d)

        if not batch:
            print(
                f"[{name}] {s}: all {t.num_rows:,} websites already have favicons",
                flush=True,
            )
            continue

        # Process batch with thread pool
        ok = small = fail = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            fut_map = {ex.submit(worker, source, d): d for d in batch}
            done_count = 0
            for fut in concurrent.futures.as_completed(fut_map):
                r = fut.result()
                if r == "ok":
                    ok += 1
                elif r == "small":
                    small += 1
                else:
                    fail += 1
                done_count += 1

                # Heartbeat: flush progress every HEARTBEAT seconds
                now = time.time()
                if now - last_heartbeat >= HEARTBEAT:
                    elapsed = now - t0
                    total = total_ok + ok + total_small + small + total_fail + fail
                    rate = total / elapsed if elapsed > 0 else 0
                    with open(source["log"], "w") as lf:
                        lf.write(
                            f"state={s} ok={total_ok + ok:,} "
                            f"small={total_small + small:,} "
                            f"fail={total_fail + fail:,} "
                            f"elapsed={elapsed:.0f}s rate={rate:.1f}/s\n"
                        )
                    last_heartbeat = now

        total_ok += ok
        total_small += small
        total_fail += fail
        elapsed = time.time() - t0
        rate = (total_ok + total_small + total_fail) / elapsed if elapsed > 0 else 0
        print(
            f"[{name}] {s}: {len(batch):,} domains → ok={ok:,} small={small:,} "
            f"fail={fail:,} cum={total_ok:,}/{total_small:,}/{total_fail:,} "
            f"elapsed={elapsed:.0f}s rate={rate:.1f}/s",
            flush=True,
        )

        # Record progress for resume
        with open(STATE_ORDER_FILE, "w") as f:
            f.write(s + "\n")

    elapsed = time.time() - t0
    rate = (total_ok + total_small + total_fail) / elapsed if elapsed > 0 else 0
    print(
        f"[{name}] DONE: ok={total_ok:,} small={total_small:,} "
        f"fail={total_fail:,} elapsed={elapsed:.0f}s rate={rate:.1f}/s",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Parallel favicon downloader")
    parser.add_argument("--source", choices=["google", "ddg", "both"], default="both")
    parser.add_argument("--workers", type=int, default=25)
    parser.add_argument("--resume", action="store_true", default=True)
    args = parser.parse_args()

    sources = []
    if args.source in ("google", "both"):
        sources.append(("google", SOURCE_CONFIG["google"]))
    if args.source in ("ddg", "both"):
        sources.append(("ddg", SOURCE_CONFIG["ddg"]))

    if len(sources) == 1:
        run_source(sources[0][0], sources[0][1], args.workers, args.resume)
    else:
        # Both sources: run in separate processes (multiprocessing)
        import multiprocessing

        procs = []
        for name, cfg in sources:
            p = multiprocessing.Process(
                target=run_source, args=(name, cfg, args.workers, args.resume)
            )
            p.start()
            procs.append(p)
        for p in procs:
            p.join()


if __name__ == "__main__":
    main()
