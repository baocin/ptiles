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
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from ptiles.codec import read_header
from ptiles.scopes import country_of, publish_relpath

# Scope is a country/state code with an optional subdivision, so that a country
# split across several files (JP-KANTO, JP-KANSAI) is expressible. This used to
# be [A-Z]{2}, which skipped every such file with only a note on stderr.
FILE_RE = re.compile(
    r"^(?P<scope>[A-Z]{2}(?:-[A-Z0-9]+)?)\.(?P<layer>[a-z_]+?)(?:_v(?P<ver>\d+))?\.ptiles$"
)


def _describe_ptlr(path):
    """Metadata for a PTLR roads file.

    PTLR is a different container from the H3-block layers: 256-byte header,
    three zoom-band ZSTD frames, no spatial index and -- importantly -- no
    bounding box. read_header() does not reject it, it reads PTLR's bytes
    through the PTILES field layout, which yielded a feature count of
    471450137651052544 and bounds of ~1e-39 for every roads file published so
    far. Parse the real fields instead, and report bounds as null rather than
    inventing them.
    """
    data = path.read_bytes()[:80]
    version = data[4]
    road_count = struct.unpack_from("<I", data, 56)[0]
    out = {
        "format": "PTLR",
        "format_version": version,
        "features": road_count,
        "blocks": 3,  # the three zoom bands
        "bounds": None,  # PTLR stores none; a client must not filter on it
    }
    if version < 2:
        # v1 wrote the Z04 count (motorway/trunk/primary only) into the total
        # field, so this undercounts the file by roughly 95%. Say so rather than
        # publishing it as the road total.
        out["features_partial"] = "z04_only"
    return out


def describe(path, scope):
    head = path.read_bytes()[:4]
    if head == b"PTLR":
        detail = _describe_ptlr(path)
    else:
        with open(path, "rb") as f:
            h = read_header(f)
        detail = {
            "features": h["feature_count"],
            "blocks": h["block_count"],
            "bounds": [
                round(h["min_lat"], 6), round(h["min_lon"], 6),
                round(h["max_lat"], 6), round(h["max_lon"], 6),
            ],
        }
    return {
        "country": country_of(scope),
        # Where this file sits inside the published snapshot. The client joins
        # it onto the snapshot URL rather than reconstructing the layout rule.
        "path": publish_relpath(scope, path.name),
        "bytes": path.stat().st_size,
        **detail,
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
            # Fatal, not a warning. A skipped file is absent from the manifest,
            # so the deploy publishes a snapshot missing a layer and every step
            # still reports success -- which is how a whole country's buildings
            # went unnoticed.
            print(f"FATAL: unrecognised .ptiles name: {path.name}", file=sys.stderr)
            sys.exit(1)
        layer = m["layer"]
        entry = layers.setdefault(
            layer, {"version": int(m["ver"]) if m["ver"] else None, "scopes": {}}
        )
        if entry["version"] != (int(m["ver"]) if m["ver"] else None):
            # Two versions of one layer in a single build would make the file
            # name unpredictable from the manifest, which is the whole point.
            print(f"FATAL: {layer} has mixed versions in this build", file=sys.stderr)
            sys.exit(1)
        entry["scopes"][m["scope"]] = describe(path, m["scope"])

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
            cm = FILE_RE.match(f.name)
            if not cm:
                # Carried files used to bypass FILE_RE entirely by splitting on
                # the first dot, so a malformed name reached the manifest.
                print(f"FATAL: unrecognised carried name: {f.name}", file=sys.stderr)
                sys.exit(1)
            entry["scopes"][cm["scope"]] = describe(f, cm["scope"])

    for layer, entry in layers.items():
        v = entry["version"]
        # Kept for older clients. `pattern` cannot express the published layout
        # (US at the snapshot root, other countries under a country directory),
        # so a client that can should join each scope's own `path` instead.
        entry["pattern"] = f"{{scope}}.{layer}_v{v}.ptiles" if v else f"{{scope}}.{layer}.ptiles"
        entry["count"] = len(entry["scopes"])
        entry["bytes"] = sum(s["bytes"] for s in entry["scopes"].values())
        entry["countries"] = sorted({s["country"] for s in entry["scopes"].values()})

    # country -> scopes, so a client can open "everything for Japan" without
    # having to know that JP buildings are regional and JP places are not.
    countries: dict[str, set] = {}
    for entry in layers.values():
        for scope, s in entry["scopes"].items():
            countries.setdefault(s["country"], set()).add(scope)

    json.dump(
        {
            "built": built,
            "source": source,
            "countries": {c: sorted(s) for c, s in sorted(countries.items())},
            "layers": dict(sorted(layers.items())),
            "total_bytes": sum(e["bytes"] for e in layers.values()),
        },
        sys.stdout,
        indent=2,
    )
    print()


if __name__ == "__main__":
    main()
