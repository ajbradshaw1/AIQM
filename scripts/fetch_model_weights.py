#!/usr/bin/env python3
"""Fetch and verify the model weights that are distributed outside Git.

The weights are 203 MB across 74 files, of which the 82.6 MB DINOv2 encoder
appears twice, byte-identical. Committing them put that on every clone
forever — Git history is append-only, so a blob added once cannot be made
cheap later without rewriting shared history. They now live beside the rest
of the lab's data and are fetched on demand.

``models/WEIGHTS_MANIFEST.json`` records every path with its sha256 and
size, generated from the files as they were committed. Nothing here trusts
a filename: a file is accepted only when its digest matches, so a truncated
copy or a wrong-model mix-up fails loudly instead of producing quietly
wrong classifications.

Two roles:

* ``deployed``  — 37 files, 101 MB. Loaded at runtime by
  gui/weak_primary_shadow.py. Required to run the shadow model.
* ``benchmark`` — 37 files, 101 MB. Research artifacts backing the
  published numbers. Not needed to run the GUI.

``--role deployed`` is the default because that is what a lab machine
needs.

Usage:

    # From a mounted Synology share, or any directory laid out like the repo
    python scripts/fetch_model_weights.py --source /Volumes/research/aiqm-weights

    # Everything, including the benchmark artifacts
    python scripts/fetch_model_weights.py --source <dir> --role all

    # Check what is already on disk without copying anything
    python scripts/fetch_model_weights.py --verify-only

The source directory must mirror the manifest's relative paths — i.e. it
contains ``models/weak_primary_lambda_0_1/...``. Copy the ``models/`` tree
there verbatim and this script will find everything.

Read-only with respect to the source. Writes only under the repository's
``models/`` directory, and never overwrites a file that already verifies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "models" / "WEIGHTS_MANIFEST.json"
_READ_BLOCK = 8 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_READ_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest() -> dict:
    if not MANIFEST_PATH.is_file():
        raise SystemExit(
            f"Manifest not found: {MANIFEST_PATH}\n"
            "This script needs it to know what to fetch and what each file "
            "should hash to."
        )
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise SystemExit(
            f"Unsupported manifest schema_version: "
            f"{manifest.get('schema_version')!r}"
        )
    return manifest


def select(manifest: dict, role: str) -> list[dict]:
    files = manifest["files"]
    if role == "all":
        return files
    return [entry for entry in files if entry["role"] == role]


def verify_one(entry: dict) -> tuple[bool, str]:
    """Return (ok, reason). A present-but-wrong file is not ok."""
    target = REPO_ROOT / entry["path"]
    if not target.is_file():
        return False, "missing"
    actual_size = target.stat().st_size
    if actual_size != entry["size_bytes"]:
        # Cheap discriminator before hashing 83 MB: a truncated copy is the
        # common failure when a transfer is interrupted.
        return False, f"wrong size ({actual_size} != {entry['size_bytes']})"
    if sha256_file(target) != entry["sha256"]:
        return False, "sha256 mismatch"
    return True, "ok"


def do_verify(entries: list[dict]) -> int:
    bad: list[tuple[str, str]] = []
    for entry in entries:
        ok, reason = verify_one(entry)
        if not ok:
            bad.append((entry["path"], reason))
    print(f"{len(entries) - len(bad)}/{len(entries)} files verified")
    for path, reason in bad:
        print(f"  {reason:32} {path}")
    if bad:
        print(
            "\nFetch the missing or corrupt files with:\n"
            "  python scripts/fetch_model_weights.py --source <dir>"
        )
        return 1
    return 0


def do_fetch(entries: list[dict], source: Path, force: bool) -> int:
    if not source.is_dir():
        raise SystemExit(f"--source is not a directory: {source}")

    copied = skipped = 0
    failures: list[tuple[str, str]] = []
    for entry in entries:
        target = REPO_ROOT / entry["path"]
        ok, _reason = verify_one(entry)
        if ok and not force:
            # Already correct. Re-copying 83 MB to reach the same bytes is
            # only a way to lose them to an interrupted write.
            skipped += 1
            continue

        candidate = source / entry["path"]
        if not candidate.is_file():
            failures.append((entry["path"], f"not in source: {candidate}"))
            continue
        if candidate.stat().st_size != entry["size_bytes"]:
            failures.append((entry["path"], "source file has the wrong size"))
            continue
        if sha256_file(candidate) != entry["sha256"]:
            # Verify BEFORE writing: a bad file that never lands cannot be
            # half-fetched into the tree and mistaken for a good one later.
            failures.append((entry["path"], "source sha256 mismatch"))
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, target)
        ok, reason = verify_one(entry)
        if not ok:
            failures.append((entry["path"], f"after copy: {reason}"))
            continue
        copied += 1
        print(f"  fetched  {entry['path']}")

    print(f"\n{copied} copied, {skipped} already present, {len(failures)} failed")
    for path, reason in failures:
        print(f"  FAILED  {reason:40} {path}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", type=Path,
        help="Directory mirroring the repo layout (e.g. a mounted Synology "
             "share). Required unless --verify-only.",
    )
    parser.add_argument(
        "--role", choices=("deployed", "benchmark", "all"), default="deployed",
        help="Which weights to act on (default: deployed — what the GUI "
             "needs to run).",
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="Check what is already on disk; copy nothing.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-copy even files that already verify.",
    )
    args = parser.parse_args()

    manifest = load_manifest()
    entries = select(manifest, args.role)
    total_mb = sum(e["size_bytes"] for e in entries) / 1048576
    print(f"{len(entries)} {args.role} file(s), {total_mb:.0f} MB\n")

    if args.verify_only:
        return do_verify(entries)
    if args.source is None:
        parser.error("--source is required unless --verify-only is given")
    return do_fetch(entries, args.source.resolve(), args.force)


if __name__ == "__main__":
    sys.exit(main())
