#!/usr/bin/env python3
"""Build a CS-team-friendly dataset from Growth Monitor sessions.

Two operating modes:

  ``--catalog-only``
    Scans ``logs/growths/`` (or ``--logs-root``) and writes
    ``catalog.json`` — an inventory row per session with counts +
    duration + quality flags. Fast; no file copying. Use to send
    the CS team an "what data exists?" summary before a full bundle.

  full bundle (default)
    Same catalog scan, then produces a ``.tar.gz`` containing the
    catalog + per-session CSVs in a normalized ``sessions/<id>/``
    structure + a schema doc + a README. Optionally includes the
    RHEED frame BMPs (``--include-frames``); otherwise every session
    directory in the tar carries a ``frame_manifest.json`` mapping
    CSV rows to their referenced frame paths in the original repo.

Quality flags surface downstream selection choices to the CS team
rather than making the cut here. Every session gets tagged with zero
or more of: ``empty`` / ``dummy`` / ``startup_test`` / ``real_growth``
/ ``has_labels`` / ``has_classifier_data`` / ``long_duration``. See
``assess_quality()`` for the thresholds.

Usage:
    # Catalog only (fast)
    python scripts/build_cs_dataset.py --catalog-only \\
        --output-dir /tmp/aiqm_catalog/

    # Full bundle
    python scripts/build_cs_dataset.py \\
        --output-dir /tmp/aiqm_bundle/ \\
        --bundle-name aiqm_bundle_2026_07

    # Full bundle including all RHEED frame BMPs
    python scripts/build_cs_dataset.py \\
        --output-dir /tmp/aiqm_bundle/ --include-frames

Author-time note: the tool reuses the SessionArtifacts reader shipped
in growth_profile_explorer.py (Day 4) so schema drift only happens
in one place. If GrowthLogger.SENSOR_FIELDS et al. change, both this
tool and the explorer pick up the change without edits.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.growth_profile_explorer import SessionArtifacts  # noqa: E402
from gui.recon_labels import RECON_LABELS  # noqa: E402


# Quality-flag threshold defaults. Kept as module-level constants so
# they're discoverable + testable independently of the assessment code.
STARTUP_TEST_MAX_SECONDS = 120       # < 2 min → startup_test
LONG_DURATION_MIN_SECONDS = 600      # ≥ 10 min → long_duration
# real_growth requires (min duration OR any grower interaction).
REAL_GROWTH_MIN_SECONDS = 120


@dataclass
class CatalogEntry:
    """One row in the CS-team dataset catalog.

    All fields are JSON-serializable primitives so the catalog can
    round-trip through catalog.json without dataclass-specific
    handling on the CS side.
    """
    session_id: str
    sample_id: str
    grower: str
    start_iso: str
    duration_s: float
    duration_str: str
    counts: dict[str, int]
    quality_flags: list[str]
    has_classifier_data: bool
    has_grower_labels: bool
    path_rel: str


def _first_row_field(rows: list[dict], key: str, default: str = "") -> str:
    """First non-blank ``key`` value across ``rows``, or ``default``."""
    for row in rows:
        v = row.get(key, "")
        if v is not None and str(v).strip():
            return str(v).strip()
    return default


def _session_start_iso(artifacts: SessionArtifacts) -> str:
    """Earliest timestamp we can find in this session.

    Preference order: first sensor row → first heartbeat → first
    commit → session_dir mtime as a last-resort fallback. Returns
    ISO-8601 string or empty string if nothing usable exists.
    """
    for source in (artifacts.sensor_rows, artifacts.heartbeat_rows,
                   artifacts.commit_rows):
        if source:
            ts = source[0].get("timestamp", "").strip()
            if ts:
                return ts
    try:
        mtime = artifacts.session_dir.stat().st_mtime
        return datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def _duration_seconds(artifacts: SessionArtifacts) -> float:
    """Session duration in seconds from the last elapsed_s in any source."""
    from scripts.growth_profile_explorer import _safe_float
    best = 0.0
    for source in (artifacts.sensor_rows, artifacts.heartbeat_rows,
                   artifacts.commit_rows):
        if source:
            t = _safe_float(source[-1].get("elapsed_s"))
            if t is not None and t > best:
                best = t
    return best


def _duration_str(seconds: float) -> str:
    if seconds < 1.0:
        return "—"
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}m {secs}s"


def _has_classifier_data(commit_rows: list[dict]) -> bool:
    """True iff any commit row has a non-empty classifier_recon_1x1 cell.

    Uses 1x1 as the sentinel — if classifier ran at all, this column
    is populated. Empty across all rows means classifier was DISABLED
    or ERROR the whole session.
    """
    for row in commit_rows:
        v = row.get("classifier_recon_1x1", "")
        if v is not None and str(v).strip():
            return True
    return False


def _has_grower_labels(artifacts: SessionArtifacts) -> bool:
    """True iff the grower produced ANY reconstruction label this session.

    Covers all three labeling surfaces per docs/equalizer_labeling_sop.md:
    Events tab (events_labels.csv), Live Equalizer (live_labels.csv),
    Monitor tab grower_corrected commits (commit_log.csv).
    """
    if artifacts.event_label_rows:
        return True
    # live_labels.csv isn't in SessionArtifacts (not exposed by
    # explorer's reader) — check the file directly on disk.
    live_path = artifacts.session_dir / "live_labels.csv"
    if live_path.exists() and live_path.stat().st_size > 100:
        # >100 bytes means at least one non-header row (header ~100
        # bytes; empty CSV would be just the header line)
        return True
    for row in artifacts.commit_rows:
        if row.get("grower_corrected", "").strip() == "True":
            return True
    return False


def _looks_dummy(artifacts: SessionArtifacts) -> bool:
    """Heuristic: sensor readings look like DummyPyrometer output.

    DummyPyrometer emits a fixed synthetic profile — pyrometer_temp_C
    values sit within a narrow band around a fixed setpoint. Real
    readings vary. If ALL sensor rows are within a 0.5°C window of
    each other, flag as dummy.

    Not foolproof (a real growth held at exactly one temperature
    might trigger a false positive), but useful as a first-pass tag
    for excluding synthetic Mac-side test sessions from training.
    """
    from scripts.growth_profile_explorer import _safe_float
    temps = [
        _safe_float(row.get("pyrometer_temp_C"))
        for row in artifacts.sensor_rows
    ]
    temps = [t for t in temps if t is not None]
    if len(temps) < 5:
        return False
    return max(temps) - min(temps) < 0.5


def assess_quality(artifacts: SessionArtifacts,
                    duration_s: float) -> list[str]:
    """Return the list of quality flags for one session.

    Flags are tags, not filters. The CS team decides which flags mean
    "include" for their pipeline. Encoded here so the definitions
    live in one place and stay testable independently of the tar
    writer.
    """
    flags: list[str] = []
    grower_interacted = bool(
        artifacts.commit_rows or artifacts.manual_event_rows
        or artifacts.auto_capture_rows or artifacts.event_label_rows
    )

    if not artifacts.sensor_rows and not grower_interacted:
        flags.append("empty")
        return flags

    if _looks_dummy(artifacts):
        flags.append("dummy")

    if duration_s < STARTUP_TEST_MAX_SECONDS and not grower_interacted:
        flags.append("startup_test")

    if duration_s >= REAL_GROWTH_MIN_SECONDS or grower_interacted:
        # "real_growth" is a permissive tag — either long enough OR
        # showed grower engagement. Startup tests won't hit either.
        flags.append("real_growth")

    if duration_s >= LONG_DURATION_MIN_SECONDS:
        flags.append("long_duration")

    if _has_grower_labels(artifacts):
        flags.append("has_labels")

    if _has_classifier_data(artifacts.commit_rows):
        flags.append("has_classifier_data")

    return flags


def catalog_session(session_dir: Path) -> Optional[CatalogEntry]:
    """Read one session dir → catalog entry.

    Returns ``None`` if the session dir doesn't parse cleanly — e.g.
    it's not really a session dir (no CSVs) or the reader raises.
    Callers should log the skip; malformed dirs shouldn't sink the
    whole catalog walk.
    """
    try:
        artifacts = SessionArtifacts.from_session_dir(session_dir)
    except (FileNotFoundError, OSError):
        return None

    duration_s = _duration_seconds(artifacts)
    rheed_view_rows = 0
    rheed_view_path = session_dir / "rheed_view_events.csv"
    try:
        if rheed_view_path.exists():
            with rheed_view_path.open(
                newline="", encoding="utf-8"
            ) as stream:
                rheed_view_rows = sum(1 for _ in csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error):
        return None

    counts = {
        "sensor_rows": len(artifacts.sensor_rows),
        "commit_rows": len(artifacts.commit_rows),
        "auto_capture_rows": len(artifacts.auto_capture_rows),
        "manual_event_rows": len(artifacts.manual_event_rows),
        "heartbeat_rows": len(artifacts.heartbeat_rows),
        "rheed_view_event_rows": rheed_view_rows,
        "event_label_rows": len(artifacts.event_label_rows),
    }

    return CatalogEntry(
        session_id=session_dir.name,
        sample_id=_first_row_field(artifacts.commit_rows, "sample_id"),
        grower=_first_row_field(artifacts.commit_rows, "grower"),
        start_iso=_session_start_iso(artifacts),
        duration_s=round(duration_s, 2),
        duration_str=_duration_str(duration_s),
        counts=counts,
        quality_flags=assess_quality(artifacts, duration_s),
        has_classifier_data=_has_classifier_data(artifacts.commit_rows),
        has_grower_labels=_has_grower_labels(artifacts),
        path_rel=session_dir.name,
    )


def scan_catalog(logs_root: Path) -> list[CatalogEntry]:
    """Walk ``logs_root``, produce a catalog entry per session dir.

    Session dirs are identified by naming convention: name starts
    with ``growth_`` (matches ``GrowthLogger.start_session``'s
    ``{prefix}_{safe_id}_{tag}`` format with prefix="growth").
    """
    if not logs_root.is_dir():
        raise FileNotFoundError(f"logs root not found: {logs_root}")
    entries: list[CatalogEntry] = []
    for path in sorted(logs_root.iterdir()):
        if not path.is_dir():
            continue
        if not path.name.startswith("growth_"):
            continue
        entry = catalog_session(path)
        if entry is not None:
            entries.append(entry)
    return entries


def write_catalog_json(entries: list[CatalogEntry],
                        output_path: Path) -> Path:
    """Serialize the catalog to ``output_path`` (creates parents)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_iso": datetime.now().isoformat(timespec="seconds"),
        "generator": "scripts/build_cs_dataset.py",
        "session_count": len(entries),
        "sessions": [asdict(e) for e in entries],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return output_path


# CSVs to copy per session. Order controls bundling; missing files
# are silently skipped (older sessions predate later additions).
_BUNDLED_CSVS: list[str] = [
    "sensor_log.csv",
    "commit_log.csv",
    "auto_capture_events.csv",
    "manual_events.csv",
    "rheed_view_events.csv",
    "heartbeat_log.csv",
    "events_labels.csv",
    "live_labels.csv",
    "set_change_events.csv",
    "session_metadata.json",
]


def _frame_paths_for_session(session_dir: Path) -> list[Path]:
    """Collect every RHEED frame path referenced by any CSV in a session.

    Reads commit_log.csv:frame_path, manual_events.csv:frame_path,
    rheed_view_events.csv:frame_path, heartbeat_log.csv:frame_path,
    live_labels.csv:frame_path and auto_capture_events.csv:buffer_dir/*.bmp.
    Deduplicates.
    Returns absolute paths that still exist on disk.
    """
    seen: set[Path] = set()

    def _add(p_str: str):
        p_str = (p_str or "").strip()
        if not p_str:
            return
        p = Path(p_str)
        if not p.is_absolute():
            p = session_dir / p
        if p.exists():
            seen.add(p.resolve())

    for csv_name, col in (
        ("commit_log.csv", "frame_path"),
        ("manual_events.csv", "frame_path"),
        ("rheed_view_events.csv", "frame_path"),
        ("heartbeat_log.csv", "frame_path"),
        ("live_labels.csv", "frame_path"),
    ):
        p = session_dir / csv_name
        if not p.exists():
            continue
        with open(p, newline="") as f:
            for row in csv.DictReader(f):
                _add(row.get(col, ""))

    # auto_capture: buffer_dir points to a subdirectory of BMPs
    ac_path = session_dir / "auto_capture_events.csv"
    if ac_path.exists():
        with open(ac_path, newline="") as f:
            for row in csv.DictReader(f):
                bd = (row.get("buffer_dir") or "").strip()
                if not bd:
                    continue
                bd_path = Path(bd)
                if not bd_path.is_absolute():
                    bd_path = session_dir / bd
                if bd_path.is_dir():
                    for bmp in bd_path.glob("*.bmp"):
                        seen.add(bmp.resolve())

    return sorted(seen)


def _frame_manifest(session_dir: Path,
                     frame_paths: list[Path]) -> dict:
    """JSON blob describing where the session's frames LIVE.

    For CSV-only bundles, this replaces the actual frame files so the
    CS team can pull the frames themselves if they have filesystem
    access to the source. Paths are stored relative to the original
    session dir so downstream can compose them against any base.
    """
    rels: list[str] = []
    for p in frame_paths:
        try:
            rel = p.resolve().relative_to(session_dir.resolve())
            rels.append(str(rel))
        except ValueError:
            # Frame lives outside the session dir (unusual). Store
            # absolute — CS team can resolve.
            rels.append(str(p))
    return {
        "session_id": session_dir.name,
        "frame_count": len(rels),
        "frames_relative_to_session_dir": rels,
    }


def bundle_sessions(
    entries: list[CatalogEntry],
    logs_root: Path,
    output_tar: Path,
    schema_doc_path: Optional[Path] = None,
    include_frames: bool = False,
) -> Path:
    """Assemble a .tar.gz bundle from ``entries``.

    Structure inside the tar:

        <bundle_root>/
          catalog.json
          schema.md   (copy of docs/cs_team_bundle_format.md if provided)
          README.md   (auto-generated)
          sessions/
            <session_id>/
              sensor_log.csv, commit_log.csv, ... (only those present)
              session_meta.json  (subset + our quality flags)
              frame_manifest.json  (path list; only if include_frames=False)
              frames/*.bmp  (only if include_frames=True)

    ``bundle_root`` is derived from ``output_tar``'s stem, so
    ``aiqm_bundle_2026_07.tar.gz`` unpacks to a folder named
    ``aiqm_bundle_2026_07/``.
    """
    output_tar.parent.mkdir(parents=True, exist_ok=True)
    bundle_root = output_tar.name.rsplit(".tar", 1)[0]

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / bundle_root
        staging.mkdir()

        # catalog.json at bundle root
        write_catalog_json(entries, staging / "catalog.json")

        # schema.md — copy if provided, else emit a stub pointer
        target_schema = staging / "schema.md"
        if schema_doc_path is not None and schema_doc_path.exists():
            shutil.copy2(schema_doc_path, target_schema)
        else:
            target_schema.write_text(
                "# Schema\n\nSee docs/cs_team_bundle_format.md in the "
                "AIQM repo for the full column reference.\n",
                encoding="utf-8",
            )

        # README.md — usage summary
        (staging / "README.md").write_text(
            _bundle_readme(entries, include_frames),
            encoding="utf-8",
        )

        sessions_dir = staging / "sessions"
        sessions_dir.mkdir()

        for entry in entries:
            src = logs_root / entry.path_rel
            if not src.is_dir():
                continue
            dst = sessions_dir / entry.session_id
            dst.mkdir()

            for csv_name in _BUNDLED_CSVS:
                s = src / csv_name
                if s.exists():
                    shutil.copy2(s, dst / csv_name)

            # session_meta.json — small subset + our quality tags
            meta = asdict(entry)
            with open(dst / "session_meta.json", "w",
                      encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

            frame_paths = _frame_paths_for_session(src)
            if include_frames:
                # Copy every referenced BMP into <bundle>/sessions/
                # <id>/frames/, preserving basenames only. If two
                # BMPs share a basename (heartbeat vs buffer), the
                # buffer copy wins by iteration order — this is a
                # limitation we accept for v1.
                (dst / "frames").mkdir()
                for fp in frame_paths:
                    try:
                        shutil.copy2(fp, dst / "frames" / fp.name)
                    except OSError:
                        continue
            else:
                # Manifest replaces the BMPs; caller can pull frames
                # themselves from the source repo.
                with open(dst / "frame_manifest.json", "w",
                          encoding="utf-8") as f:
                    json.dump(_frame_manifest(src, frame_paths),
                              f, indent=2)

        # Roll the staging dir into the tar.gz
        with tarfile.open(output_tar, "w:gz") as tar:
            tar.add(staging, arcname=bundle_root)

    return output_tar


def _bundle_readme(entries: list[CatalogEntry],
                    include_frames: bool) -> str:
    """One-page README embedded at bundle root.

    Kept text-only + minimal so the CS team sees session counts +
    quality-flag totals at a glance without opening catalog.json.
    """
    total = len(entries)
    flag_counts: dict[str, int] = {}
    for e in entries:
        for f in e.quality_flags:
            flag_counts[f] = flag_counts.get(f, 0) + 1

    lines = [
        f"# AIQM dataset bundle",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Session count: {total}",
        f"Frames included: {'yes' if include_frames else 'no (see frame_manifest.json per session)'}",
        "",
        "## Quality flag totals",
        "",
    ]
    for flag in sorted(flag_counts):
        lines.append(f"- **{flag}**: {flag_counts[flag]}")
    lines += [
        "",
        "## Layout",
        "",
        "```",
        "catalog.json           # index of all included sessions",
        "schema.md              # full column reference",
        "sessions/<session_id>/",
        "  {sensor,commit,auto_capture,manual,heartbeat,rheed_view_events}.csv",
        "  {events_labels,live_labels,set_change_events}.csv",
        "  session_meta.json    # counts + quality flags",
    ]
    if include_frames:
        lines.append("  frames/*.bmp         # RHEED frames referenced by CSVs")
    else:
        lines.append("  frame_manifest.json  # paths to frames in the source repo")
    lines += [
        "```",
        "",
        "## Getting started",
        "",
        "1. Read `catalog.json` to filter by quality flags.",
        "2. Per-session CSVs use the schemas documented in `schema.md`.",
        "3. Join labels (`events_labels.csv`, `live_labels.csv`, "
        "`commit_log.csv:recon_*`) on `event_idx` or `timestamp` as needed.",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Build a CS-team dataset bundle from Growth Monitor sessions."
        ),
    )
    parser.add_argument(
        "--logs-root", type=Path,
        default=REPO_ROOT / "logs" / "growths",
        help="Directory of growth_* session dirs (default: logs/growths/)",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        help="Directory to write catalog.json + bundle tar.gz",
    )
    parser.add_argument(
        "--bundle-name", type=str, default=None,
        help=(
            "Base name for the tar.gz (default: aiqm_bundle_<UTC-date>). "
            "Ignored in --catalog-only mode."
        ),
    )
    parser.add_argument(
        "--catalog-only", action="store_true",
        help="Write catalog.json only; skip tar assembly.",
    )
    parser.add_argument(
        "--include-frames", action="store_true",
        help=(
            "Include RHEED frame BMPs in the bundle (default: emit "
            "frame_manifest.json per session instead)."
        ),
    )
    parser.add_argument(
        "--exclude-quality", nargs="*", default=[],
        help=(
            "Skip sessions carrying any of these quality flags. "
            "Example: --exclude-quality empty dummy startup_test"
        ),
    )
    args = parser.parse_args()

    try:
        entries = scan_catalog(args.logs_root)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.exclude_quality:
        excluded = set(args.exclude_quality)
        entries = [
            e for e in entries
            if not (excluded & set(e.quality_flags))
        ]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = args.output_dir / "catalog.json"
    write_catalog_json(entries, catalog_path)

    print(f"Wrote catalog with {len(entries)} sessions to {catalog_path}")

    if args.catalog_only:
        return

    bundle_name = args.bundle_name or (
        f"aiqm_bundle_{datetime.now().strftime('%Y%m%d')}"
    )
    output_tar = args.output_dir / f"{bundle_name}.tar.gz"
    schema_doc = REPO_ROOT / "docs" / "cs_team_bundle_format.md"
    bundle_sessions(
        entries,
        args.logs_root,
        output_tar,
        schema_doc_path=schema_doc if schema_doc.exists() else None,
        include_frames=args.include_frames,
    )
    size_mb = output_tar.stat().st_size / (1024 * 1024)
    print(f"Wrote bundle {output_tar.name} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
