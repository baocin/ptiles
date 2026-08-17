#!/usr/bin/env python3
"""Publish a build directory to R2 as a dated snapshot, with its manifest.

    publish_snapshot.py <build_dir> <YYYY-MM-DD> <source_label> [--dry-run]

Layout published:

    maps/{date}/manifest.json
    maps/{date}/TN.buildings_v9.ptiles          # US at the snapshot root
    maps/{date}/JP/JP.places_v1.ptiles          # other countries in a directory
    maps/{date}/JP/JP-KANTO.buildings_v9.ptiles

The US stays at the root because the live map and the external JS client already
read `maps/{date}/{ST}.{layer}.ptiles`; moving those objects would break
consumers that live outside this repo. Every other country gets a directory, so
listing one country does not mean listing 51 states first.

The per-file key comes from ptiles.scopes.publish_relpath -- the same function
gen_manifest.py records in each scope's `path` -- so the manifest and the
uploaded keys cannot drift apart.

This supersedes deploy_roads.py, which uploaded only roads, filtered on a
15-character filename (so only two-letter scopes), and wrote flat to `maps/`
with no date prefix.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from ptiles.scopes import publish_relpath, scope_of_filename

S3_BUCKET = "mydatatimeline"
S3_PREFIX = "maps"
AWS_PROFILE = "mdt-r2"


def plan(build_dir: Path, date: str) -> list[tuple[Path, str]]:
    """(local file, S3 key) for everything in the build directory."""
    out = []
    for path in sorted(build_dir.rglob("*.ptiles")):
        scope = scope_of_filename(path.name)
        try:
            rel = publish_relpath(scope, path.name)
        except ValueError:
            print(f"FATAL: {path.name} does not start with a scope", file=sys.stderr)
            sys.exit(1)
        out.append((path, f"{S3_PREFIX}/{date}/{rel}"))
    return out


def upload(local: Path, key: str, content_type: str | None = None) -> bool:
    cmd = ["aws", "s3", "cp", str(local), f"s3://{S3_BUCKET}/{key}",
           "--profile", AWS_PROFILE]
    if content_type:
        cmd += ["--content-type", content_type]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  FAILED {key}: {r.stderr.strip()}", file=sys.stderr)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("build_dir")
    ap.add_argument("date", help="snapshot date, YYYY-MM-DD")
    ap.add_argument("source_label", help="e.g. 'osm-2026-08-07'")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the keys that would be written, upload nothing")
    ap.add_argument("--manifest", help="use this manifest.json instead of generating one")
    a = ap.parse_args()

    build_dir = Path(a.build_dir)
    if not build_dir.is_dir():
        sys.exit(f"not a directory: {build_dir}")

    items = plan(build_dir, a.date)
    if not items:
        sys.exit(f"no .ptiles files under {build_dir}")

    by_country: dict[str, int] = {}
    for _, key in items:
        rest = key.split(f"{a.date}/", 1)[1]
        by_country["US" if "/" not in rest else rest.split("/")[0]] = (
            by_country.get("US" if "/" not in rest else rest.split("/")[0], 0) + 1
        )
    print(f"{len(items)} files: " + ", ".join(f"{c}={n}" for c, n in sorted(by_country.items())))

    # Generate the manifest from the same tree, so it describes exactly what is
    # being uploaded. gen_manifest exits non-zero on an unrecognised name.
    if a.manifest:
        manifest_text = Path(a.manifest).read_text()
    else:
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "gen_manifest.py"),
             str(build_dir), a.date, a.source_label],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            sys.exit(f"manifest generation failed:\n{r.stderr}")
        manifest_text = r.stdout
    manifest = json.loads(manifest_text)

    # The manifest's own paths must match the keys about to be written.
    declared = {
        s["path"]
        for layer in manifest["layers"].values()
        for s in layer["scopes"].values()
    }
    planned = {key.split(f"{a.date}/", 1)[1] for _, key in items}
    if declared != planned:
        only_manifest = sorted(declared - planned)[:5]
        only_upload = sorted(planned - declared)[:5]
        sys.exit(
            "manifest and upload plan disagree:\n"
            f"  in manifest only: {only_manifest}\n"
            f"  in upload only:   {only_upload}"
        )

    if a.dry_run:
        for _, key in items:
            print(f"  s3://{S3_BUCKET}/{key}")
        print(f"  s3://{S3_BUCKET}/{S3_PREFIX}/{a.date}/manifest.json")
        print(f"\nDry run: nothing uploaded. {len(items)} files + manifest.")
        return

    ok = 0
    for local, key in items:
        if upload(local, key):
            ok += 1
            print(f"  {key}")
    manifest_path = build_dir / "manifest.json"
    manifest_path.write_text(manifest_text)
    upload(manifest_path, f"{S3_PREFIX}/{a.date}/manifest.json", "application/json")
    print(f"\nUploaded {ok}/{len(items)} files + manifest to "
          f"s3://{S3_BUCKET}/{S3_PREFIX}/{a.date}/")
    if ok != len(items):
        sys.exit(1)


if __name__ == "__main__":
    main()
