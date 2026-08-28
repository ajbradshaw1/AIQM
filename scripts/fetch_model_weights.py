#!/usr/bin/env python3
"""Verify tracked model weights and optionally restore them from a local source.

The repository keeps 74 model files under models/. The accompanying
models/WEIGHTS_MANIFEST.json binds each path to an exact byte count and
SHA-256 digest so truncated files and wrong-checkpoint substitutions fail
closed.

The shadow_runtime role is used only by the optional weak-primary shadow
route. Its execution scope remains weak_shadow_only: verifying or restoring
these files does not deploy a model and does not make its output actionable.

With no arguments, this command only verifies the shadow-runtime files already
in the checkout:

    python scripts/fetch_model_weights.py

To verify all recorded artifacts:

    python scripts/fetch_model_weights.py --role all

Recovery is opt-in and local. The user must explicitly provide a directory
whose layout mirrors the repository; this script contains no downloader and
never chooses a source from configuration or the network:

    python scripts/fetch_model_weights.py --source D:\\approved\\aiqm-weights

Already-correct targets are always skipped. A missing or corrupt target is
replaced only after the source file and a same-directory temporary copy both
match the manifest. The final replacement is atomic within the filesystem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import string
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_ROOT = REPO_ROOT / "models"
MANIFEST_PATH = MODELS_ROOT / "WEIGHTS_MANIFEST.json"
_READ_BLOCK = 8 << 20
WEIGHT_ROLES = ("shadow_runtime", "benchmark")


def _manifest_parts(rel_path: str) -> tuple[str, ...]:
    """Return canonical POSIX path parts or stop on an unsafe manifest path."""
    if not isinstance(rel_path, str) or not rel_path:
        raise SystemExit(f"Manifest path must be a non-empty string: {rel_path!r}")
    if "\\" in rel_path:
        raise SystemExit(
            f"Manifest path must use forward slashes only: {rel_path!r}"
        )
    path = PurePosixPath(rel_path)
    raw_parts = rel_path.split("/")
    if (
        path.is_absolute()
        or Path(rel_path).is_absolute()
        or any(part in ("", ".", "..") for part in raw_parts)
        or not raw_parts
        or raw_parts[0] != "models"
        or path.suffix.lower() != ".pth"
    ):
        raise SystemExit(
            f"Manifest path must be a relative .pth path under models/: "
            f"{rel_path!r}"
        )
    return tuple(path.parts)


def resolve_entry_path(rel_path: str) -> Path:
    """Resolve a manifest target, refusing paths or links outside models/."""
    parts = _manifest_parts(rel_path)
    models_root = MODELS_ROOT.resolve()
    resolved = REPO_ROOT.joinpath(*parts).resolve()
    if not resolved.is_relative_to(models_root):
        raise SystemExit(
            f"Manifest path escapes models/: {rel_path!r} -> {resolved}"
        )
    return resolved


def resolve_source_entry_path(source: Path, rel_path: str) -> Path:
    """Resolve a recovery candidate without following links outside source."""
    parts = _manifest_parts(rel_path)
    source_root = source.resolve()
    candidate = source_root.joinpath(*parts).resolve()
    if not candidate.is_relative_to(source_root):
        raise SystemExit(
            f"Source path escapes the explicit source: {rel_path!r} -> "
            f"{candidate}"
        )
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_READ_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise SystemExit("Manifest root must be a JSON object")
    if manifest.get("schema_version") != 1:
        raise SystemExit(
            "Unsupported manifest schema_version: "
            f"{manifest.get('schema_version')!r}"
        )
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SystemExit("Manifest files must be a non-empty list")

    seen: set[str] = set()
    role_counts = {role: 0 for role in WEIGHT_ROLES}
    role_bytes = {role: 0 for role in WEIGHT_ROLES}
    hex_digits = set(string.hexdigits.lower())
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise SystemExit(f"Manifest entry {index} must be an object")
        missing = {"path", "sha256", "size_bytes", "role"} - entry.keys()
        if missing:
            raise SystemExit(
                f"Manifest entry {index} is missing: {sorted(missing)}"
            )
        rel_path = entry["path"]
        resolve_entry_path(rel_path)
        if rel_path in seen:
            raise SystemExit(f"Duplicate manifest path: {rel_path}")
        seen.add(rel_path)

        digest = entry["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest != digest.lower()
            or any(char not in hex_digits for char in digest)
        ):
            raise SystemExit(f"Invalid SHA-256 for {rel_path!r}")
        size = entry["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise SystemExit(f"Invalid size_bytes for {rel_path!r}: {size!r}")
        if entry["role"] not in WEIGHT_ROLES:
            raise SystemExit(
                f"Invalid role for {rel_path!r}: {entry['role']!r}"
            )
        role_counts[entry["role"]] += 1
        role_bytes[entry["role"]] += size

    roles = manifest.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(WEIGHT_ROLES):
        raise SystemExit(
            "Manifest roles must declare exactly: "
            + ", ".join(WEIGHT_ROLES)
        )
    for role in WEIGHT_ROLES:
        if not isinstance(roles[role], str) or not roles[role].strip():
            raise SystemExit(f"Manifest role description is empty: {role}")
        if role_counts[role] == 0:
            raise SystemExit(
                f"Manifest role {role!r} must contain at least one file"
            )

    expected_totals = {
        "files": len(files),
        "bytes": sum(role_bytes.values()),
        "shadow_runtime_files": role_counts["shadow_runtime"],
        "shadow_runtime_bytes": role_bytes["shadow_runtime"],
    }
    totals = manifest.get("totals")
    if not isinstance(totals, dict) or set(totals) != set(expected_totals):
        raise SystemExit(
            "Manifest totals must contain exactly: "
            + ", ".join(expected_totals)
        )
    for field, expected in expected_totals.items():
        declared = totals[field]
        if isinstance(declared, bool) or not isinstance(declared, int):
            raise SystemExit(
                f"Manifest totals.{field} must be an integer, got "
                f"{declared!r}"
            )
        if declared != expected:
            raise SystemExit(
                f"Manifest totals.{field} mismatch: "
                f"declared {declared}, computed {expected}"
            )
    return manifest


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        raise SystemExit(
            f"Manifest not found: {MANIFEST_PATH}\n"
            "Verification and recovery both require the checked-in manifest."
        )
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read manifest {MANIFEST_PATH}: {exc}") from exc
    return _validate_manifest(manifest)


def select(manifest: dict[str, Any], role: str) -> list[dict[str, Any]]:
    if role == "all":
        selected = list(manifest["files"])
    else:
        if role not in WEIGHT_ROLES:
            raise SystemExit(f"Unknown model-weight role: {role!r}")
        selected = [
            entry for entry in manifest["files"] if entry["role"] == role
        ]
    if not selected:
        raise SystemExit(f"No model-weight files selected for role {role!r}")
    return selected


def verify_one(entry: dict[str, Any]) -> tuple[bool, str]:
    """Return (ok, reason); present-but-wrong files are never accepted."""
    target = resolve_entry_path(entry["path"])
    if not target.is_file():
        return False, "missing"
    actual_size = target.stat().st_size
    if actual_size != entry["size_bytes"]:
        return False, f"wrong size ({actual_size} != {entry['size_bytes']})"
    if sha256_file(target) != entry["sha256"]:
        return False, "sha256 mismatch"
    return True, "ok"


def do_verify(entries: list[dict[str, Any]]) -> int:
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
            "\nNo files were changed. To restore from a reviewed local copy, "
            "rerun with an explicit --source <directory>."
        )
        return 1
    return 0


def do_fetch(entries: list[dict[str, Any]], source: Path) -> int:
    """Restore invalid targets from one explicit, repository-shaped source."""
    try:
        source = source.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"--source cannot be resolved: {source}: {exc}") from exc
    if not source.is_dir():
        raise SystemExit(f"--source is not a directory: {source}")

    copied = 0
    skipped = 0
    failures: list[tuple[str, str]] = []
    for entry in entries:
        target = resolve_entry_path(entry["path"])
        ok, _reason = verify_one(entry)
        if ok:
            skipped += 1
            continue

        try:
            candidate = resolve_source_entry_path(source, entry["path"])
        except SystemExit as exc:
            failures.append((entry["path"], str(exc)))
            continue
        if not candidate.is_file():
            failures.append((entry["path"], f"not in source: {candidate}"))
            continue
        if candidate.stat().st_size != entry["size_bytes"]:
            failures.append((entry["path"], "source file has the wrong size"))
            continue
        if sha256_file(candidate) != entry["sha256"]:
            failures.append((entry["path"], "source sha256 mismatch"))
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".partial",
        )
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            shutil.copy2(candidate, temp_path)
            if (
                temp_path.stat().st_size != entry["size_bytes"]
                or sha256_file(temp_path) != entry["sha256"]
            ):
                failures.append((entry["path"], "temporary copy did not verify"))
                continue
            os.replace(temp_path, target)
        except Exception as exc:  # noqa: BLE001
            failures.append((entry["path"], f"copy failed: {exc}"))
            continue
        finally:
            temp_path.unlink(missing_ok=True)
        copied += 1
        print(f"  restored {entry['path']}")

    print(
        f"\n{copied} restored, {skipped} already correct, "
        f"{len(failures)} failed"
    )
    for path, reason in failures:
        print(f"  FAILED  {reason:40} {path}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        type=Path,
        help=(
            "Explicit local directory mirroring the repository layout. "
            "Omit to verify only; no source is inferred and no network is used."
        ),
    )
    parser.add_argument(
        "--role",
        choices=(*WEIGHT_ROLES, "all"),
        default="shadow_runtime",
        help=(
            "Weights to verify or restore (default: shadow_runtime; "
            "this is shadow-only, not deployment)."
        ),
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify existing files and forbid recovery even if --source is set.",
    )
    args = parser.parse_args(argv)

    if args.verify_only and args.source is not None:
        parser.error("--verify-only cannot be combined with --source")

    manifest = load_manifest()
    entries = select(manifest, args.role)
    total_mib = sum(entry["size_bytes"] for entry in entries) / 1048576
    action = "restore/verify" if args.source is not None else "verify"
    print(
        f"{len(entries)} {args.role} file(s), {total_mib:.0f} MiB; "
        f"mode={action}\n"
    )

    if args.source is None:
        return do_verify(entries)
    return do_fetch(entries, args.source)


if __name__ == "__main__":
    sys.exit(main())
