#!/usr/bin/env python3
"""Write manifest.json for a build directory.

The published prefix is the build date alone (maps/YYYY-MM-DD/); the layer
version stays in the file name, because that is the wire format the decoder
switches on and it moves per layer, not per build. The manifest is what ties
the two together, so a client reads the version from here instead of hardcoding
_v9 and needing a release every time a layer bumps.

    gen_manifest.py <build_dir> <built_date> <source_label> \
        [--carry layer_vN=/path/to/dir[:note]] > manifest.json

--carry names a layer this build did not produce but the published snapshot
still serves, because it was copied forward from an earlier build. Roads is the
usual case: it is expensive to rebuild and changes slowly, so a snapshot carries
the previous vintage rather than dropping the layer. The entry is marked
`carried_forward` so the manifest never implies it was built on `built`.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from ptiles.codec import read_header

FILE_RE = re.compile(r"^(?P<scope>[A-Z]{2})\.(?P<layer>[a-z_]+?)(?:_v(?P<ver>\d+))?\.ptiles$")


def describe(path):
    with open(path, "rb") as f:
        h = read_header(f)
    return {
        "bytes": path.stat().st_size,
        "features": h["feature_count"],
        "blocks": h["block_count"],
        "bounds": [
            round(h["min_lat"], 6), round(h["min_lon"], 6),
            round(h["max_lat"], 6), round(h["max_lon"], 6),
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("build_dir")
    ap.add_argument("built_date")
    ap.add_argument("source_label")
    ap.add_argument("--carry", action="append", default=[],
                    metavar="layer_vN=DIR[:note]",
                    help="a layer this build did not produce but the snapshot serves")
    a = ap.parse_args()
    build_dir, built, source = Path(a.build_dir), a.built_date, a.source_label
    carries = a.carry

    layers = {}
    for path in sorted(build_dir.rglob("*.ptiles")):
        m = FILE_RE.match(path.name)
        if not m:
            print(f"skipping unrecognised name: {path.name}", file=sys.stderr)
            continue
        layer = m["layer"]
        entry = layers.setdefault(
            layer, {"version": int(m["ver"]) if m["ver"] else None, "scopes": {}}
        )
        if entry["version"] != (int(m["ver"]) if m["ver"] else None):
            # Two versions of one layer in a single build would make the file
            # name unpredictable from the manifest, which is the whole point.
            print(f"FATAL: {layer} has mixed versions in this build", file=sys.stderr)
            sys.exit(1)
        entry["scopes"][m["scope"]] = describe(path)

    # Layers carried forward from an earlier build, served by this snapshot but
    # not produced by it.
    for carry in carries:
        spec, _, note = carry.partition(":")
        name_ver, _, path = spec.partition("=")
        layer, _, ver = name_ver.rpartition("_v")
        if not layer or not ver.isdigit():
            print(f"FATAL: --carry needs layer_vN=dir, got {name_ver!r}", file=sys.stderr)
            sys.exit(1)
        src = Path(path)
        if not src.is_dir():
            print(f"FATAL: --carry dir not found: {src}", file=sys.stderr)
            sys.exit(1)
        entry = layers.setdefault(layer, {"version": int(ver), "scopes": {}})
        entry["carried_forward"] = True
        if note:
            entry["note"] = note
        for f in sorted(src.glob("*.ptiles")):
            scope = f.name.split(".")[0]
            entry["scopes"][scope] = describe(f)

    for layer, entry in layers.items():
        v = entry["version"]
        entry["pattern"] = f"{{scope}}.{layer}_v{v}.ptiles" if v else f"{{scope}}.{layer}.ptiles"
        entry["count"] = len(entry["scopes"])
        entry["bytes"] = sum(s["bytes"] for s in entry["scopes"].values())

    json.dump(
        {
            "built": built,
            "source": source,
            "layers": dict(sorted(layers.items())),
            "total_bytes": sum(e["bytes"] for e in layers.values()),
        },
        sys.stdout,
        indent=2,
    )
    print()


if __name__ == "__main__":
    main()
