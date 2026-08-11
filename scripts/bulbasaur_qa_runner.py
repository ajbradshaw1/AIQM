#!/usr/bin/env python3
"""Bulbasaur validation runner — walks the queued QA items.

Purpose: on the next lab-open day, `python scripts/bulbasaur_qa_runner.py`
should be the first thing the grower runs. It walks the 13-item
Bulbasaur validation queue that's accumulated across Jul 10 workstream
+ Jul 13-24 sprint items, presenting each item's context and steps,
recording pass/fail + notes to a timestamped markdown report.

Design decisions:

  - Interactive REPL, not TUI. Growers want to run this alongside
    the actual OMBE lab work — a text-only prompt-and-response
    loop stays out of the way and works over SSH if needed.
  - Saves after every item so a crashed session or Ctrl-C loses
    at most the current item's notes.
  - Resume support via ``--resume <report_path>`` — reads the
    partial report, skips any items already answered.
  - No repository state changes. This tool records observations;
    fixes get committed separately.

Usage:
    # Fresh run — writes bulbasaur_qa_<UTC-date>.md in cwd
    python scripts/bulbasaur_qa_runner.py

    # Custom output path
    python scripts/bulbasaur_qa_runner.py \\
        --output /tmp/qa_report_2026_07_25.md

    # Resume a partial run
    python scripts/bulbasaur_qa_runner.py \\
        --resume /tmp/qa_report_2026_07_25.md
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------
# Validation queue definition
# --------------------------------------------------------------------------
@dataclass
class ValidationItem:
    """One item on the Bulbasaur QA checklist."""
    id: str  # short unique key, used in the report
    title: str
    area: str  # gui / drivers / scripts — for report grouping
    shipped_ref: str  # commit hash or date reference
    description: str
    steps: list[str]
    expected: str


# The queue. Ordered roughly by dependency + physical setup
# convenience — items that share a session or a tab get grouped so
# the grower doesn't jump around.
VALIDATION_QUEUE: list[ValidationItem] = [
    # -- Jul 10 workstream ---------------------------------------------
    ValidationItem(
        id="jul10_mark_event",
        title="MARK EVENT button",
        area="gui",
        shipped_ref="80b87c7 (Jul 10 workstream #1)",
        description=(
            "One-tap grower marker button on the Monitor tab. Writes "
            "one row to manual_events.csv + snaps a BMP into frames/. "
            "Amber (#d97706) identity."
        ),
        steps=[
            "Start a session (any config)",
            "Click MARK EVENT once — verify amber flash + row in "
            "footer counter",
            "Click 3-4 more times during the session",
            "Check logs/growths/<session>/manual_events.csv exists "
            "with matching row count",
            "Verify frames/manual_NNN_*.bmp files exist per click",
        ],
        expected=(
            "1 CSV row + 1 BMP per click. Amber footer counter "
            "increments in real time. No blocking on the UI thread."
        ),
    ),
    ValidationItem(
        id="jul10_continuous_capture",
        title="Continuous-capture surface + Monitor indicator",
        area="gui",
        shipped_ref="1e3e9e3 (Jul 10 workstream #2)",
        description=(
            "Renamed 'heartbeat' → 'continuous capture' surface + "
            "cyan (#0891b2) indicator in Monitor tab footer showing "
            "capture cadence + frame count."
        ),
        steps=[
            "Start a session with heartbeat_interval=5s (default)",
            "Watch the Monitor footer for the cyan indicator",
            "Wait 30s — indicator should show ~6 frames captured",
            "Check heartbeat_log.csv has matching row count",
        ],
        expected=(
            "'Continuous: every 5s · N frames' in Monitor footer. "
            "Cyan color matches scrubber marker convention."
        ),
    ),
    ValidationItem(
        id="jul10_scrubber_tab_v1",
        title="Scrubber tab v1 — timeline playback",
        area="gui",
        shipped_ref="65bde1a (Jul 10 workstream #3)",
        description=(
            "New Scrubber tab. Combined heartbeat + manual + "
            "auto-capture timeline. Per-source colored markers on "
            "the slider. Lazy QPixmap frame load."
        ),
        steps=[
            "Start a session; run for ~2 minutes with a mix of "
            "MARK EVENT clicks and heartbeats",
            "Switch to Scrubber tab — verify slider populates",
            "Drag through the timeline — verify frames render "
            "with reasonable latency (<200ms)",
            "Verify marker colors: cyan=heartbeat, amber=manual",
            "Click ↻ Reload — verify new heartbeats picked up",
        ],
        expected=(
            "Slider spans all captured frames. Metadata line under "
            "image updates with elapsed/pyro/V/I. No memory spike "
            "(scrubbing 100 frames should stay <200MB RAM growth)."
        ),
    ),
    ValidationItem(
        id="jul10_scrubber_auto_poll",
        title="Scrubber live auto-poll (Day 8 addition)",
        area="gui",
        shipped_ref="241cd9e (Day 8 sprint C4)",
        description=(
            "5-second QTimer refreshes scrubber index during "
            "active session. Race guard defers reload if grower "
            "scrubbed within last 3s."
        ),
        steps=[
            "During an active session, open Scrubber tab",
            "Wait 30s WITHOUT clicking ↻ Reload — verify slider "
            "range extends as new heartbeats land",
            "Verify Reload button label reads '↻ Reload (auto)'",
            "Drag the slider actively, wait exactly 5s — reload "
            "should NOT interrupt (race guard fires)",
            "Stop session — verify label reverts to '↻ Reload'",
        ],
        expected=(
            "Auto-refresh works while grower is idle. Race guard "
            "prevents scrub-stealing during active drag. Label + "
            "tooltip clearly indicate active state."
        ),
    ),
    ValidationItem(
        id="jul10_movie_export",
        title="Movie export — time-lapse mp4",
        area="gui",
        shipped_ref="e738aed (Jul 10 workstream #5)",
        description=(
            "Post-session movie export using cv2.VideoWriter mp4v "
            "codec. Encodes heartbeat frames into a time-lapse."
        ),
        steps=[
            "End a session that captured ≥30 heartbeat frames",
            "Session tab → click Export Movie",
            "Wait for encoding to complete (~5s for 100 frames)",
            "Open the resulting .mp4 in QuickTime / VLC",
            "Verify playback + frame count matches heartbeat_log.csv",
        ],
        expected=(
            "MP4 opens cleanly. Frame count = heartbeat row count. "
            "No stalls or partial writes."
        ),
    ),
    ValidationItem(
        id="jul10_live_equalizer_tab",
        title="Live Equalizer tab — real-time RHEED labeling",
        area="gui",
        shipped_ref="b631e3d (Jul 10 workstream #4)",
        description=(
            "New Live Equalizer tab. 2×2 layout: Selected / "
            "Constructed / Classifier % / Grower %. Auto-fit, "
            "Save writes live_labels.csv + BMP snapshot."
        ),
        steps=[
            "Start a session with live camera",
            "Switch to Live Equalizer tab",
            "Verify Selected pane shows live frames",
            "Click Auto-fit — Constructed pane populates",
            "Drag a slider — Constructed pane updates live",
            "Click Save label — verify live_labels.csv row + BMP",
        ],
        expected=(
            "All 4 panes populate. Auto-fit produces reasonable "
            "starting mixture. Save latency <500ms."
        ),
    ),
    # -- Jul 13-14 sprint days -----------------------------------------
    ValidationItem(
        id="day1_scaling_image_refactor",
        title="ScalingImageLabel refactor (Day 1)",
        area="gui",
        shipped_ref="d530ae1 (Day 1 D1)",
        description=(
            "Shared ScalingImageLabel widget used by Events tab + "
            "Scrubber tab. Consolidated from two duplicate classes. "
            "Regression risk if display sizing shifted."
        ),
        steps=[
            "Open Events tab, click into a captured event → verify "
            "buffer preview image displays at expected size",
            "Open Scrubber tab → verify frame image displays at "
            "expected size (larger than Events)",
            "Resize main window — both should rescale smoothly",
        ],
        expected=(
            "Events tab uses 320×240 minimum. Scrubber uses 480×360 "
            "minimum. No image cropping or aspect-ratio distortion."
        ),
    ),
    ValidationItem(
        id="day2_freeze_frame",
        title="Freeze frame button on Live Equalizer (Day 2 C2)",
        area="gui",
        shipped_ref="41147bb (Day 2 C2)",
        description=(
            "Live Equalizer pause button. Amber (#d97706) matching "
            "MARK EVENT identity. Freezes Selected pane so grower "
            "can label a specific frame without chase."
        ),
        steps=[
            "Live Equalizer tab, session running with live camera",
            "Click Freeze frame — button turns amber, label switches "
            "to 'Resume live'",
            "Verify Selected pane STOPS updating with new frames",
            "Adjust sliders / click Auto-fit / Save — should work "
            "against frozen frame",
            "Click Resume live — pane starts updating again",
            "End session, restart — pause state cleared",
        ],
        expected=(
            "Frozen frame stays frozen. Save operates on frozen "
            "frame. Reset clears pause state. Amber color matches "
            "MARK EVENT."
        ),
    ),
    ValidationItem(
        id="day3_change_from_to_dropdowns",
        title="Events tab change_from/to dropdowns (Day 3 C1)",
        area="gui",
        shipped_ref="96743b7 (Day 3 C1)",
        description=(
            "Two new dropdowns on Events tab labeling form: "
            "Change (from) → Change (to). Feeds Yuxin's #1 "
            "active-comparisons pipeline as transition labels."
        ),
        steps=[
            "End a session with ≥3 auto-capture events",
            "Events tab → select an event",
            "In labeling form, set Change (from) = 1x1, "
            "Change (to) = Twinned (2x1)",
            "Verify events_labels.csv row has both columns populated",
            "Close + reopen app — verify dropdowns still show "
            "the same values",
        ],
        expected=(
            "Atomic write per dropdown (no Save button). Persists "
            "across app restart. Does not affect the primary_recon "
            "dropdown."
        ),
    ),
    ValidationItem(
        id="day4_6_growth_profile_explorer",
        title="Growth Profile Explorer against real session (Day 4-6 B1)",
        area="scripts",
        shipped_ref="8368160 (Day 6 B1 Part 3)",
        description=(
            "Post-hoc analysis tool. 5 PNG panels + self-contained "
            "HTML report. Never run against a real Bulbasaur session "
            "with classifier + labels."
        ),
        steps=[
            "Pick a completed session dir with commit_log + "
            "classifier data",
            "Run: python scripts/growth_profile_explorer.py "
            "logs/growths/<session>/",
            "Verify 5 PNGs land in analysis/ subdir",
            "Verify growth_profile_report.html opens in browser",
            "Check metadata header shows grower / sample / duration",
            "Try --stride 4 on a long session — verify smaller "
            "PNGs, no HTML broken",
        ],
        expected=(
            "All 5 charts render. HTML self-contained (email test: "
            "attach + open in webmail). --stride works cleanly."
        ),
    ),
    # -- Direct-read + kSA + MISTRAL -----------------------------------
    ValidationItem(
        id="jul9_direct_read_elog",
        title="Direct-read Elog tab live at Bulbasaur",
        area="drivers",
        shipped_ref="e6f5f5d + 74bddcd (Jul 9)",
        description=(
            "Reads EvapControl .elo binary log directly (not via "
            "screengrab OCR). Populates sensor_log.csv "
            "substrate_temp_pv_C + cell_*_pv_C + plasma_* columns."
        ),
        steps=[
            "Ensure EvapControl running + writing to .elo file",
            "Session tab → Config → set Evap Control = elog "
            "(should be default as of 74bddcd)",
            "Start a session",
            "Session tab → Direct-read tab: verify substrate + cell "
            "values updating in real-time",
            "Check sensor_log.csv — new columns populated (not empty)",
        ],
        expected=(
            "Values match what MISTRAL displays for the same cells. "
            "Values update at ~1Hz. No stalls when EvapControl "
            "rotates the log file."
        ),
    ),
    ValidationItem(
        id="jul9_ksa_comm_client",
        title="KsaCommClient live TCP against kSA",
        area="drivers",
        shipped_ref="58b7393 (Jul 9)",
        description=(
            "Reusable kSA v6 wire protocol client. Never exercised "
            "against a real kSA 400 on Bulbasaur."
        ),
        steps=[
            "kSA 400 must be running with TCP/IP interface enabled",
            "Run: python scripts/test_ksa_single.py "
            "(needs live kSA)",
            "Verify handshake completes",
            "Verify sample TEXT_CMD query returns expected reply",
            "Check log for any 'unlock required' error phrases",
        ],
        expected=(
            "Connection + handshake succeeds. Text commands round-"
            "trip. Error taxonomy (5 known phrases) matches "
            "ksa_tcp_ip_breakthrough memory."
        ),
    ),
    ValidationItem(
        id="jul9_mistral_jsonrpc",
        title="MISTRAL JSON-RPC 2.0 client against Kestrel",
        area="drivers",
        shipped_ref="c8fc482 + 9d8ee18 (Jul 2)",
        description=(
            "JSON-RPC 2.0 client for MISTRAL Kestrel backend. "
            "Scaffold only — not shipped as production PSU source. "
            "Validation: does it CONNECT + return valid replies?"
        ),
        steps=[
            "Verify MISTRAL Kestrel is running at 10.0.42.231:9000",
            "Run: python scripts/test_mistral_jsonrpc_discovery.py "
            "(needs live Kestrel)",
            "Verify probe queries return valid JSON-RPC replies",
            "Check no unexpected errors in stderr",
        ],
        expected=(
            "Connection succeeds. All probes return valid replies. "
            "Multi-client safe (can run alongside MistralGui)."
        ),
    ),
    # -- Research probes (kSA + Vimba direct-read) ---------------------
    ValidationItem(
        id="jul15_ksa_textcmd_image_probes",
        title="Path B — kSA TEXT_CMD image-export probes",
        area="drivers",
        shipped_ref=(
            "scripts/probe_ksa_image_export.py "
            "(Codex-authored, adopted Jul 16 2026)"
        ),
        description=(
            "Probe kSA's TEXT_CMD scripting language for image-export "
            "commands. If any succeed, we have a concurrent-with-kSA "
            "camera read path (VmbCamera's exclusive-lock constraint "
            "goes away). See docs/ksa_camera_research_findings.md §Path B. "
            "Caveat: kSA advertises 8-bit or 10-bit programmable data "
            "output — exported files may NOT preserve full 12-bit "
            "precision even if the command works."
        ),
        steps=[
            "kSA 400 must be running with acquisition open + a live "
            "image visible",
            "cd to the AIQM repo on Bulbasaur",
            "python scripts/probe_ksa_image_export.py "
            "--out-dir C:\\temp\\aiqm_ksa_probe "
            "--result-json C:\\temp\\aiqm_ksa_probe\\result.json",
            "Inspect result.json for per-probe: err code, reply text, "
            "and whether an image file landed on disk",
            "For any file that landed: check dtype + min/max +"
            "observed bit depth (script includes this in the JSON)",
            "If AIQM_KSA_EXPORT_COMMAND env var route is discovered, "
            "note the working command template for the next step",
        ],
        expected=(
            "AT LEAST ONE probe succeeds with err=0 AND writes a "
            "valid image. Success → next-step: wire KsaImageExporter "
            "driver into rheed_camera.py (Codex has a stub). Failure "
            "on ALL → move to item #15 (Path D: VmbPy AccessMode.Read)."
        ),
    ),
    ValidationItem(
        id="jul16_vimba_access_mode_probes",
        title="Path D — VmbPy AccessMode.Read coexistence probes",
        area="drivers",
        shipped_ref=(
            "scripts/probe_vimba_access_modes.py "
            "(Codex-authored, adopted Jul 16 2026)"
        ),
        description=(
            "Probe VmbPy's AccessMode.Read path — Allied Vision's "
            "documented mode for passive frame reception while "
            "another app owns the camera (kSA). If this works, "
            "we have concurrent-with-kSA camera access without "
            "needing kSA's scripting language OR GigE multi-cast "
            "reconfig. See docs/ksa_camera_research_findings.md "
            "§Path D. This is Codex's discovery from Jul 15-16 "
            "investigation that AJ's yesterday research missed."
        ),
        steps=[
            "PHASE 1 (kSA open, camera in kSA's control):",
            "  python scripts/probe_vimba_access_modes.py "
            "--result-json C:\\temp\\aiqm_vimba\\inventory.json",
            "  Confirm VmbPy imports + camera visible",
            "PHASE 2 (kSA still open):",
            "  python scripts/probe_vimba_access_modes.py "
            "--attempt-read-open --attempt-read-stream "
            "--duration-s 5 "
            "--result-json C:\\temp\\aiqm_vimba\\read.json",
            "  Success = frames received without disrupting kSA",
            "PHASE 3 (kSA CLOSED, diagnostic only):",
            "  python scripts/probe_vimba_access_modes.py "
            "--attempt-full-open --list-features "
            "--result-json C:\\temp\\aiqm_vimba\\full.json",
            "  Confirms baseline Vimba works in dedicated sessions",
        ],
        expected=(
            "PHASE 1 must pass to proceed. PHASE 2 success = Path D "
            "viable, we can build a VmbCameraReadOnly driver. "
            "PHASE 2 opens but no frames → GigE multi-cast config "
            "required (Path C). PHASE 3 always passes if Vimba "
            "installed correctly."
        ),
    ),
]


# --------------------------------------------------------------------------
# Report I/O
# --------------------------------------------------------------------------
@dataclass
class ItemResult:
    """One completed item in the QA run."""
    item_id: str
    verdict: str  # "pass" / "fail" / "skip"
    notes: str
    tested_iso: str


def _report_header(started_iso: str) -> str:
    return (
        f"# Bulbasaur validation report\n"
        f"\n"
        f"Started: {started_iso}  \n"
        f"Runner:  `scripts/bulbasaur_qa_runner.py`  \n"
        f"Queue:   {len(VALIDATION_QUEUE)} items across "
        f"gui / drivers / scripts\n"
        f"\n"
        f"---\n"
    )


def _format_item_result(item: ValidationItem,
                          result: ItemResult) -> str:
    icon = {"pass": "✅", "fail": "❌", "skip": "⏭ "}.get(
        result.verdict, "?",
    )
    return (
        f"\n## {icon} `{item.id}` — {item.title}\n"
        f"\n"
        f"**Area:** {item.area}  \n"
        f"**Shipped:** {item.shipped_ref}  \n"
        f"**Tested:** {result.tested_iso}  \n"
        f"**Verdict:** **{result.verdict.upper()}**\n"
        f"\n"
        f"**Notes:**\n"
        f"\n{result.notes or '_(no notes)_'}\n"
    )


def load_prior_results(report_path: Path) -> dict[str, ItemResult]:
    """Parse a partial report to skip items already answered.

    Very light parser — looks for section headers matching the
    format written by _format_item_result. Missing / malformed
    reports fail-open (return empty dict).
    """
    if not report_path.exists():
        return {}
    prior: dict[str, ItemResult] = {}
    current_id: Optional[str] = None
    current_verdict = ""
    current_tested = ""
    current_notes: list[str] = []
    in_notes = False

    def _flush():
        if current_id is not None:
            prior[current_id] = ItemResult(
                item_id=current_id,
                verdict=current_verdict or "skip",
                notes="\n".join(current_notes).strip(),
                tested_iso=current_tested,
            )

    for line in report_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            _flush()
            # Extract id from backticks: e.g. "## ✅ `jul10_mark_event` — ..."
            if "`" in line:
                current_id = line.split("`")[1]
            else:
                current_id = None
            current_verdict = ""
            current_tested = ""
            current_notes = []
            in_notes = False
        elif line.startswith("**Verdict:**"):
            # "**Verdict:** **PASS**"
            token = line.split("**")[-2].strip().lower()
            current_verdict = token
        elif line.startswith("**Tested:**"):
            current_tested = line.split("**Tested:**")[-1].strip()
            if current_tested.endswith("  "):
                current_tested = current_tested.rstrip()
        elif line.startswith("**Notes:**"):
            in_notes = True
        elif in_notes:
            current_notes.append(line)
    _flush()
    return prior


def append_result(report_path: Path, item: ValidationItem,
                   result: ItemResult) -> None:
    """Append one item's result to the report."""
    with open(report_path, "a", encoding="utf-8") as f:
        f.write(_format_item_result(item, result))


def initialize_report(report_path: Path) -> None:
    """Create a fresh report file with header. Overwrites existing."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(_report_header(datetime.now().isoformat(timespec="seconds")))


# --------------------------------------------------------------------------
# Interactive prompt
# --------------------------------------------------------------------------
def _print_item(idx: int, total: int, item: ValidationItem) -> None:
    print()
    print("=" * 72)
    print(f"[{idx + 1}/{total}]  {item.title}")
    print("-" * 72)
    print(f"  Area:      {item.area}")
    print(f"  Shipped:   {item.shipped_ref}")
    print()
    print("  Description:")
    for line in item.description.split("\n"):
        print(f"    {line}")
    print()
    print("  Steps:")
    for i, step in enumerate(item.steps, 1):
        print(f"    {i}. {step}")
    print()
    print("  Expected:")
    print(f"    {item.expected}")
    print("=" * 72)


def _prompt(prompt_text: str, valid: set[str]) -> str:
    while True:
        try:
            answer = input(prompt_text).strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n(interrupted)")
            sys.exit(130)
        if answer in valid:
            return answer
        print(f"  → please enter one of: {sorted(valid)}")


def prompt_verdict_and_notes(
    item: ValidationItem,
) -> Optional[ItemResult]:
    """Ask the grower for verdict + notes on ``item``. Returns None
    to signal "quit the runner cleanly, don't record this item."""
    verdict = _prompt(
        "  → verdict [p]ass / [f]ail / [s]kip / [q]uit: ",
        {"p", "f", "s", "q", "pass", "fail", "skip", "quit"},
    )
    if verdict in ("q", "quit"):
        return None
    verdict = {
        "p": "pass", "f": "fail", "s": "skip",
    }.get(verdict, verdict)

    print("  → notes (end with a blank line, empty ok):")
    lines: list[str] = []
    try:
        while True:
            line = input("    ")
            if not line.strip():
                break
            lines.append(line)
    except (KeyboardInterrupt, EOFError):
        print("\n(interrupted)")
        sys.exit(130)

    return ItemResult(
        item_id=item.id,
        verdict=verdict,
        notes="\n".join(lines).strip(),
        tested_iso=datetime.now().isoformat(timespec="seconds"),
    )


def run_qa(report_path: Path, resume: bool) -> tuple[int, int, int]:
    """Interactive loop. Returns (passed, failed, skipped) tallies."""
    prior_results: dict[str, ItemResult] = {}
    if resume and report_path.exists():
        prior_results = load_prior_results(report_path)
        print(
            f"Resuming from {report_path} "
            f"({len(prior_results)} items previously answered)"
        )
    else:
        initialize_report(report_path)
        print(f"Writing fresh report to {report_path}")

    tallies = {"pass": 0, "fail": 0, "skip": 0}
    for k, v in prior_results.items():
        tallies[v.verdict] = tallies.get(v.verdict, 0) + 1

    for idx, item in enumerate(VALIDATION_QUEUE):
        if item.id in prior_results:
            continue  # already answered
        _print_item(idx, len(VALIDATION_QUEUE), item)
        result = prompt_verdict_and_notes(item)
        if result is None:
            print("\nSaved progress. Rerun with --resume to continue.")
            break
        append_result(report_path, item, result)
        tallies[result.verdict] = tallies.get(result.verdict, 0) + 1
        print(
            f"  → recorded {result.verdict.upper()} for {item.id}"
        )

    return (
        tallies.get("pass", 0),
        tallies.get("fail", 0),
        tallies.get("skip", 0),
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Interactive Bulbasaur QA validation runner. Walks the "
            "13-item queue accumulated across Jul 10 workstream + "
            "Jul 13-24 sprint. Records pass/fail + notes to a "
            "timestamped markdown report."
        ),
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help=(
            "Report output path (default: "
            "bulbasaur_qa_<UTC-date>.md in cwd)"
        ),
    )
    parser.add_argument(
        "--resume", action="store_true",
        help=(
            "Continue a previously-started run — skips items "
            "already answered in the report file. Combine with "
            "--output pointing at the existing partial report."
        ),
    )
    args = parser.parse_args()

    report_path = args.output or (
        Path.cwd() / f"bulbasaur_qa_{datetime.now().strftime('%Y%m%d')}.md"
    )

    print(f"Queue: {len(VALIDATION_QUEUE)} items")
    print(f"Report: {report_path}")
    print("Answer p/f/s per item; q to save progress and exit.")

    passed, failed, skipped = run_qa(report_path, args.resume)
    print()
    print("=" * 72)
    print(f"Summary: {passed} pass / {failed} fail / {skipped} skip "
          f"(of {len(VALIDATION_QUEUE)} total)")
    print(f"Report saved to: {report_path}")


if __name__ == "__main__":
    main()
