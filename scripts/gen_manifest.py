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
    with open(path, "rb") as f:
        data = f.read(256)  # the whole PTLR header
    version = data[4]
    road_count = struct.unpack_from("<I", data, 56)[0]
    # Bounds live at offset 84 in micro-degrees. All-zero means the file predates
    # the field, in which case they are genuinely unknown and reported as null --
    # no real region is exactly 0/0/0/0.
    mnx, mny, mxx, mxy = struct.unpack_from("<iiii", data, 84)
    bounds = None
    if (mnx, mny, mxx, mxy) != (0, 0, 0, 0):
        bounds = [round(mny / 1e5, 6), round(mnx / 1e5, 6),
                  round(mxy / 1e5, 6), round(mxx / 1e5, 6)]
    out = {
        "format": "PTLR",
        "format_version": version,
        "features": road_count,
        "blocks": 3,  # the three zoom bands
        "bounds": bounds,
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
        entry = layers.setdefault(layer, {"scopes": {}, "_versions": {}})
        scope = m["scope"]
        meta = describe(path, scope)
        # Version is recorded per scope and summarised per country. Requiring a
        # single version per layer across the whole build was fine when a build
        # meant "the US set"; once countries are rebuilt on their own schedules
        # one country reaching buildings_v10 while another sits at v9 is normal,
        # and used to abort the publish outright.
        meta["version"] = int(m["ver"]) if m["ver"] else None
        entry["scopes"][scope] = meta
        entry["_versions"].setdefault(meta["country"], set()).add(meta["version"])

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
        entry = layers.setdefault(layer, {"scopes": {}, "_versions": {}})
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
            meta = describe(f, cm["scope"])
            meta["version"] = int(ver)
            entry["scopes"][cm["scope"]] = meta
            entry["_versions"].setdefault(meta["country"], set()).add(int(ver))

    for layer, entry in layers.items():
        per_country = entry.pop("_versions")
        # One version per country. A country building a layer at two versions in
        # one snapshot really is broken -- the filename would be unpredictable.
        for c, versions in sorted(per_country.items()):
            if len(versions) > 1:
                print(
                    f"FATAL: {layer} has versions {sorted(v or 0 for v in versions)} "
                    f"within {c} in this build",
                    file=sys.stderr,
                )
                sys.exit(1)
        entry["versions"] = {c: next(iter(v)) for c, v in sorted(per_country.items())}
        distinct = set(entry["versions"].values())
        # `version` and `pattern` only mean something when every country agrees.
        # When they differ they are null, and a client must use each scope's own
        # `path` -- which gen_manifest always records.
        v = next(iter(distinct)) if len(distinct) == 1 else None
        entry["version"] = v
        entry["pattern"] = (
            (f"{{scope}}.{layer}_v{v}.ptiles" if v else f"{{scope}}.{layer}.ptiles")
            if len(distinct) == 1
            else None
        )
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
