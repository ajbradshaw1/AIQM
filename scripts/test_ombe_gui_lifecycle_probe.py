"""Offline tests for the read-only GUI lifecycle observer.

No test starts Growth Monitor, opens an instrument interface, sends a window
message, or terminates a process.  The Windows integration test attaches a
query/wait-only handle to a benign helper that exits naturally.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import ombe_gui_lifecycle_probe as probe  # noqa: E402


def _target_sample(
    *,
    alive: bool = True,
    main_exists: bool = True,
    main_owned: bool = True,
    owner_pid: int = 4242,
    hwnd: int = 101,
) -> dict:
    main = (
        {
            "hwnd": hwnd,
            "owner_pid": owner_pid,
            "title": "Oxide MBE Growth Monitor",
            "class_name": "QtWindow",
            "visible": True,
        }
        if main_exists
        else None
    )
    return {
        "process_alive": alive,
        "process_wait_result": (
            probe.Win32Target.WAIT_TIMEOUT
            if alive
            else probe.Win32Target.WAIT_OBJECT_0
        ),
        "exit_code": None if alive else 0,
        "main_window_exists": main_exists,
        "main_window_owned_by_target": main_owned,
        "main_window": main,
        "owned_top_level_windows": [main] if main_owned and main else [],
    }


class FakeTarget:
    def __init__(self, samples: list[dict]) -> None:
        self.identity = probe.TargetIdentity(
            pid=4242,
            creation_time_utc="2026-07-29T12:00:00.000Z",
            image_path=r"C:\Python\python.exe",
            command_line=(
                r"C:\Python\python.exe D:\AI4MBE\growth_monitor_app.py"
            ),
            command_line_sha256="offline-command-hash",
            command_line_source="offline-test",
            main_hwnd=101,
            main_title="Oxide MBE Growth Monitor",
            main_class_name="QtWindow",
            requested_process_access=(
                probe.Win32Target.PROCESS_QUERY_LIMITED_INFORMATION
                | probe.Win32Target.SYNCHRONIZE
            ),
        )
        self._samples = list(samples)
        self._last = samples[-1]
        self.sample_count = 0
        self.close_count = 0

    def sample(self) -> dict:
        self.sample_count += 1
        if self._samples:
            self._last = self._samples.pop(0)
        return self._last

    def close(self) -> None:
        self.close_count += 1


class FakeSampler:
    def __init__(self, snapshots: list[probe.SessionSnapshot]) -> None:
        self._snapshots = list(snapshots)
        self._last = snapshots[-1]
        self.sample_count = 0

    def snapshot(self) -> probe.SessionSnapshot:
        self.sample_count += 1
        if self._snapshots:
            self._last = self._snapshots.pop(0)
        return self._last


class StepClock:
    def __init__(self, step_ns: int = 1_000_000_000) -> None:
        self._next_ns = 0
        self._step_ns = step_ns
        self._utc_calls = 0

    def monotonic_ns(self) -> int:
        value = self._next_ns
        self._next_ns += self._step_ns
        return value

    def now_utc(self) -> str:
        self._utc_calls += 1
        return f"2026-07-29T12:00:{self._utc_calls:02d}.000Z"


def _snapshot(
    *,
    size: int = 10,
    mtime_ns: int = 1,
    tail: str = "tail-a",
    content: str = "content-a",
) -> probe.SessionSnapshot:
    return probe.SessionSnapshot(
        entries={
            "sensor_log.csv": {
                "size": size,
                "mtime_ns": mtime_ns,
                "tail_sha256": tail,
                "content_sha256": content,
            }
        },
        errors=(),
    )


def _observe_args(
    output_dir: Path,
    *,
    role: str,
    session_dir: Path | None = None,
    require_session_advance: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        pid=4242,
        hwnd=101,
        expected_title=None,
        expected_image=None,
        expected_command_token="growth_monitor",
        target_role=role,
        scenario_id="offline-lifecycle-test",
        fault_style="graceful",
        test_context="offline-tabletop",
        gui_state=None,
        operator="offline-operator",
        observer="offline-observer",
        approver="offline-approver",
        authorization_ref="offline-authorization",
        sop_ref="offline-sop",
        safe_state_note="synthetic process only; no instrument interfaces",
        abort_criteria="any unexpected external process or interface access",
        confirm_safe_state=True,
        require_action_markers=False,
        require_recovery_validation=False,
        paired_failure_run=None,
        session_dir=session_dir,
        require_session_advance=require_session_advance or [],
        sample_interval_s=0.01,
        duration_s=20.0,
        baseline_duration_s=1.0,
        post_loss_observation_s=2.0,
        post_loss_settle_s=0.5,
        orphan_grace_s=10.0,
        output_dir=output_dir,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_marker(
    output_dir: Path,
    *,
    name: str,
    kind: str,
    monotonic_ns: int,
    at_utc: str,
    boot_id: str = "boot-test",
    actor: str | None = None,
    evidence_ref: str | None = None,
) -> None:
    marker_dir = output_dir / "markers"
    marker_dir.mkdir(exist_ok=True)
    _write_json(
        marker_dir / f"{name}.json",
        {
            "marker_id": name,
            "at_utc": at_utc,
            "monotonic_ns": monotonic_ns,
            "boot_id": boot_id,
            "pid": 111,
            "kind": kind,
            "label": kind,
            "operator": actor or (
                "offline-observer"
                if kind == "precheck-complete"
                else "offline-operator"
            ),
            "new_pid": None,
            "new_session_dir": None,
            "evidence_ref": evidence_ref,
        },
    )


def _summary_fixture(
    output_dir: Path,
    *,
    role: str = "growth-monitor",
    session_dir: str | None = None,
    late_change: dict | None = None,
    require_recovery_validation: bool = False,
) -> None:
    output_dir.mkdir()
    fixture_boot_id = probe.current_boot_identity()[0]
    _write_json(
        output_dir / "run_info.json",
        {
            "target_role": role,
            "fault_style": "graceful",
            "test_context": "offline-tabletop",
            "gui_state": None,
            "require_action_markers": True,
            "require_recovery_validation": require_recovery_validation,
            "observer_boot_id": fixture_boot_id,
            "observer_pid": 111,
            "operator": "offline-operator",
            "observer": "offline-observer",
            "approver": "offline-approver",
            "sample_interval_s": 0.5,
            "duration_s": 10.0,
            "baseline_duration_s": 1.0,
            "post_loss_observation_s": 2.0,
            "post_loss_settle_s": 0.5,
            "orphan_grace_s": 2.0,
            "session_dir": session_dir,
            "require_session_advance": [],
            "target_identity": {
                "pid": 4242,
                "creation_time_utc": "2026-07-29T12:00:00.000Z",
                "image_path": r"C:\Python\python.exe",
                "command_line": (
                    r"C:\Python\python.exe "
                    r"D:\AI4MBE\growth_monitor_app.py"
                ),
                "command_line_sha256": "offline-command-hash",
                "command_line_source": "offline-test",
                "main_hwnd": 101,
            },
        },
    )
    event_process_alive = role == "vendor-data-window"
    samples = [
        {
            "sample_index": 0,
            "observed_at_utc": "2026-07-29T12:00:00.000Z",
            "observed_monotonic_ns": 0,
            "target": _target_sample(),
            "artifact_changes": [],
            "session": {"errors": []},
        },
        {
            "sample_index": 1,
            "observed_at_utc": "2026-07-29T12:00:02.000Z",
            "observed_monotonic_ns": 2_000_000_000,
            "target": _target_sample(
                alive=event_process_alive,
                main_exists=False,
                main_owned=False,
            ),
            "artifact_changes": [],
            "session": {"errors": []},
        },
        {
            "sample_index": 2,
            "observed_at_utc": "2026-07-29T12:00:04.000Z",
            "observed_monotonic_ns": 4_000_000_000,
            "target": _target_sample(
                alive=event_process_alive,
                main_exists=False,
                main_owned=False,
            ),
            "artifact_changes": [late_change] if late_change else [],
            "session": {"errors": []},
        },
    ]
    _write_jsonl(output_dir / "samples.jsonl", samples)
    _write_json(
        output_dir / "run_complete.json",
        {
            "completed_at_utc": "2026-07-29T12:00:05.000Z",
            "completed_monotonic_ns": 5_000_000_000,
            "observer_boot_id": fixture_boot_id,
            "lifecycle_errors": [],
        },
    )
    _write_marker(
        output_dir,
        name="01-precheck",
        kind="precheck-complete",
        monotonic_ns=1_100_000_000,
        at_utc="2026-07-29T12:00:01.100Z",
        boot_id=fixture_boot_id,
        actor="offline-observer",
    )
    _write_marker(
        output_dir,
        name="02-close",
        kind="close-action",
        monotonic_ns=1_500_000_000,
        at_utc="2026-07-29T12:00:01.500Z",
        boot_id=fixture_boot_id,
        actor="offline-operator",
    )
    if session_dir is not None:
        session_path = Path(session_dir)
        if role == "growth-monitor":
            metadata_path = session_path / "session_metadata.json"
            if not metadata_path.exists():
                _write_json(metadata_path, {"status": "closed"})
        _write_json(
            output_dir / "session_audit.json",
            probe.audit_session(session_path),
        )


def _mark_args(
    output_dir: Path,
    *,
    kind: str,
    operator: str,
    new_pid: int | None = None,
    new_session_dir: Path | None = None,
    evidence_ref: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        output_dir=output_dir,
        kind=kind,
        label=f"offline {kind}",
        operator=operator,
        new_pid=new_pid,
        new_session_dir=new_session_dir,
        evidence_ref=evidence_ref,
        new_hwnd=None,
        new_expected_title=None,
        new_expected_image=None,
        new_expected_command_token="growth_monitor",
    )


def _restarted_target(*samples: dict) -> FakeTarget:
    target = FakeTarget(
        list(samples)
        or [_target_sample(owner_pid=5151, hwnd=202)]
    )
    target.identity = probe.TargetIdentity(
        pid=5151,
        creation_time_utc="2026-07-29T12:10:00.000Z",
        image_path=r"C:\Python\python.exe",
        command_line=(
            r"C:\Python\python.exe D:\AI4MBE\growth_monitor_app.py"
        ),
        command_line_sha256="offline-restart-command-hash",
        command_line_source="offline-test",
        main_hwnd=202,
        main_title="Oxide MBE Growth Monitor",
        main_class_name="QtWindow",
        requested_process_access=(
            probe.Win32Target.PROCESS_QUERY_LIMITED_INFORMATION
            | probe.Win32Target.SYNCHRONIZE
        ),
    )
    return target


def _vendor_rebound_target() -> FakeTarget:
    target = FakeTarget([_target_sample(owner_pid=4242, hwnd=202)])
    target.identity = probe.TargetIdentity(
        pid=4242,
        creation_time_utc="2026-07-29T12:00:00.000Z",
        image_path=r"C:\Vendor\vendor.exe",
        command_line=r"C:\Vendor\vendor.exe --live",
        command_line_sha256="offline-vendor-command-hash",
        command_line_source="offline-test",
        main_hwnd=202,
        main_title="Live Data",
        main_class_name="VendorWindow",
        requested_process_access=(
            probe.Win32Target.PROCESS_QUERY_LIMITED_INFORMATION
            | probe.Win32Target.SYNCHRONIZE
        ),
    )
    return target


class ArtifactDiffTests(unittest.TestCase):
    def test_touch_append_rewrite_and_truncate_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary)
            csv_path = session_dir / "sensor_log.csv"
            csv_path.write_bytes(b"a,b\n1,2\n")
            sampler = probe.SessionSampler(session_dir)

            original = sampler.snapshot()
            stat = csv_path.stat()
            os.utime(
                csv_path,
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000),
            )
            touched = sampler.snapshot()
            self.assertEqual(
                probe.diff_artifacts(original, touched)[0]["kind"],
                "touch-only",
            )

            with csv_path.open("ab") as stream:
                stream.write(b"3,4\n")
            appended = sampler.snapshot()
            self.assertEqual(
                probe.diff_artifacts(touched, appended)[0]["kind"],
                "appended",
            )

            csv_path.write_bytes(b"a,b\n9,8\n7,6\n")
            rewritten = sampler.snapshot()
            self.assertEqual(
                probe.diff_artifacts(appended, rewritten)[0]["kind"],
                "same-size-rewrite",
            )

            csv_path.write_bytes(b"a,b\n")
            truncated = sampler.snapshot()
            self.assertEqual(
                probe.diff_artifacts(rewritten, truncated)[0]["kind"],
                "truncated",
            )

    def test_bad_csv_tail_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sensor_log.csv"
            path.write_bytes(b'a,b\n1,"unterminated')
            result = probe.inspect_csv(path)
            self.assertFalse(result["final_newline"])
            self.assertTrue(
                any("CSV parse error" in error for error in result["errors"]),
                result,
            )

    def test_complete_row_without_final_newline_is_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sensor_log.csv"
            path.write_bytes(b"a,b\n1,2")
            result = probe.inspect_csv(path)
            self.assertEqual(result["data_row_count"], 1)
            self.assertIn(
                "file does not end with a newline",
                result["errors"],
            )

    def test_final_audit_detects_same_size_rewrite_with_restored_mtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary)
            path = session_dir / "sensor_log.csv"
            path.write_bytes(b"a,b\n1,2\n")
            original_stat = path.stat()
            real_sha256 = probe._sha256
            calls = 0

            def mutate_after_first_hash(candidate: Path) -> str:
                nonlocal calls
                calls += 1
                digest = real_sha256(candidate)
                if calls == 1:
                    candidate.write_bytes(b"a,b\n9,8\n")
                    os.utime(
                        candidate,
                        ns=(
                            original_stat.st_atime_ns,
                            original_stat.st_mtime_ns,
                        ),
                    )
                return digest

            with mock.patch.object(
                probe,
                "_sha256",
                side_effect=mutate_after_first_hash,
            ):
                audit = probe.audit_session(session_dir)

        entry = audit["file_manifest"]["sensor_log.csv"]
        self.assertTrue(entry["stable_during_hash"])
        self.assertFalse(entry["stable_through_inspection"])
        self.assertNotEqual(entry["sha256"], entry["inspection_sha256"])
        self.assertTrue(
            any(
                "changed during final inspection" in error
                for error in audit["audit_errors"]
            )
        )


class PathAndIdentityGuardTests(unittest.TestCase):
    def test_output_inside_observed_session_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session_dir = Path(temporary) / "session"
            session_dir.mkdir()
            output_dir = session_dir / "observer"
            with self.assertRaisesRegex(ValueError, "must not be inside"):
                probe.prepare_output_dir(output_dir, session_dir)
            self.assertFalse(output_dir.exists())

    def test_observer_refuses_its_own_pid_before_opening_a_handle(self) -> None:
        with mock.patch.object(probe.os, "name", "nt"):
            with self.assertRaisesRegex(ValueError, "itself"):
                probe.Win32Target(os.getpid(), hwnd=101)

    def test_abrupt_fault_is_rejected_for_live_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = _observe_args(
                Path(temporary) / "evidence",
                role="growth-monitor",
            )
            args.command = "run"
            args.fault_style = "abrupt"
            args.test_context = "live-idle-advisory"
            with self.assertRaisesRegex(ValueError, "offline-tabletop"):
                probe.validate_args(args)

    def test_safe_state_and_independent_observer_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = _observe_args(
                Path(temporary) / "evidence",
                role="growth-monitor",
            )
            args.command = "run"
            args.confirm_safe_state = False
            with self.assertRaisesRegex(ValueError, "confirm-safe-state"):
                probe.validate_args(args)
            args.confirm_safe_state = True
            args.observer = args.operator
            with self.assertRaisesRegex(ValueError, "must be different"):
                probe.validate_args(args)

    def test_nonfinite_timing_and_empty_command_token_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for field, value in (
                ("duration_s", float("nan")),
                ("sample_interval_s", float("inf")),
                ("baseline_duration_s", 0.0),
                ("orphan_grace_s", 0.0),
            ):
                with self.subTest(field=field, value=value):
                    args = _observe_args(
                        Path(temporary) / f"evidence-{field}",
                        role="growth-monitor",
                    )
                    args.command = "run"
                    setattr(args, field, value)
                    with self.assertRaises(ValueError):
                        probe.validate_args(args)

            args = _observe_args(
                Path(temporary) / "evidence-token",
                role="growth-monitor",
            )
            args.command = "run"
            args.expected_command_token = "  "
            with self.assertRaisesRegex(ValueError, "command-token"):
                probe.validate_args(args)

    def test_hwnd_and_exact_title_can_be_combined(self) -> None:
        parser = probe.build_parser()
        args = parser.parse_args([
            "run",
            "--pid",
            "4242",
            "--hwnd",
            "0x65",
            "--expected-title",
            "Oxide MBE Growth Monitor",
            "--target-role",
            "growth-monitor",
            "--scenario-id",
            "offline-parser",
            "--fault-style",
            "graceful",
            "--test-context",
            "offline-tabletop",
            "--operator",
            "operator",
            "--observer",
            "observer",
            "--approver",
            "approver",
            "--authorization-ref",
            "offline",
            "--sop-ref",
            "offline",
            "--safe-state-note",
            "no hardware",
            "--abort-criteria",
            "any unexpected access",
            "--confirm-safe-state",
            "--output-dir",
            "offline-output",
        ])
        self.assertEqual(args.hwnd, 101)
        self.assertEqual(args.expected_title, "Oxide MBE Growth Monitor")

    def test_live_gui_state_separates_idle_from_advisory_running(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = _observe_args(
                root / "running-evidence",
                role="growth-monitor",
                session_dir=root / "session",
            )
            args.command = "run"
            args.test_context = "live-idle-advisory"
            args.gui_state = "advisory-running"
            args.require_action_markers = True
            args.require_recovery_validation = True
            with self.assertRaisesRegex(
                ValueError, "require-session-advance"
            ):
                probe.validate_args(args)
            args.require_session_advance = ["sensor_log.csv"]
            probe.validate_args(args)

            args.gui_state = "idle-disarmed"
            with self.assertRaisesRegex(
                ValueError, "omit session logging"
            ):
                probe.validate_args(args)
            args.session_dir = None
            args.require_session_advance = []
            probe.validate_args(args)


class EvidenceSchemaTests(unittest.TestCase):
    def test_malformed_sample_shapes_are_evidence_fatal(self) -> None:
        mutations = {
            "target": lambda row: row.__setitem__("target", []),
            "artifact": lambda row: row.__setitem__(
                "artifact_changes", [1]
            ),
            "session": lambda row: row.__setitem__("session", []),
            "session_null": lambda row: row.__setitem__("session", None),
            "utc": lambda row: row.__setitem__(
                "observed_at_utc", "not-a-time"
            ),
            "index": lambda row: row.__setitem__("sample_index", 99),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                output_dir = Path(tmp) / "evidence"
                _summary_fixture(output_dir)
                rows, errors = probe._load_jsonl(
                    output_dir / "samples.jsonl"
                )
                self.assertEqual(errors, [])
                mutate(rows[0])
                _write_jsonl(output_dir / "samples.jsonl", rows)
                summary, code = probe.summarize_run(output_dir)
                self.assertEqual(code, probe.EXIT_EVIDENCE_FATAL)
                self.assertEqual(summary["outcome"], "evidence-fatal")
                self.assertTrue(summary["evidence_errors"])

    def test_non_object_run_info_is_evidence_fatal_not_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "evidence"
            _summary_fixture(output_dir)
            _write_json(output_dir / "run_info.json", [])
            summary, code = probe.summarize_run(output_dir)
        self.assertEqual(code, probe.EXIT_EVIDENCE_FATAL)
        self.assertEqual(summary["outcome"], "evidence-fatal")
        self.assertIn("not an object", summary["evidence_errors"][0])

    def test_malformed_nested_session_audit_is_evidence_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_dir = root / "session"
            session_dir.mkdir()
            output_dir = root / "evidence"
            _summary_fixture(
                output_dir,
                session_dir=str(session_dir),
            )
            _write_json(
                output_dir / "session_audit.json",
                {
                    "audit_errors": [],
                    "csv": {"sensor_log.csv": {"errors": 7}},
                    "json": {},
                    "image_errors": {},
                    "file_manifest": [],
                    "frame_references": {"parse_errors": 3},
                },
            )
            summary, code = probe.summarize_run(output_dir)
        self.assertEqual(code, probe.EXIT_EVIDENCE_FATAL)
        self.assertTrue(summary["evidence_errors"])

    def test_action_marker_rejected_after_sampling_phase_ends(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "evidence"
            _summary_fixture(output_dir)
            (output_dir / "run_complete.json").unlink()
            _write_json(
                output_dir / "observer_status.json",
                {
                    "observer_pid": 111,
                    "observer_boot_id": "boot-test",
                    "sampling_active": False,
                    "phase": "final-audit",
                },
            )
            args = _mark_args(
                output_dir,
                kind="precheck-complete",
                operator="offline-observer",
            )
            with (
                mock.patch.object(
                    probe._ObserverLease,
                    "is_live",
                    return_value=True,
                ),
                mock.patch.object(
                    probe,
                    "current_boot_identity",
                    return_value=("boot-test", "offline-test"),
                ),
                self.assertRaisesRegex(RuntimeError, "sampling phase has ended"),
            ):
                probe.mark_event(args)

    def test_live_action_markers_require_bound_evidence(self) -> None:
        for kind, operator in (
            ("precheck-complete", "offline-observer"),
            ("close-action", "offline-operator"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                output_dir = Path(tmp) / "evidence"
                _summary_fixture(output_dir)
                run_info_path = output_dir / "run_info.json"
                run_info = json.loads(
                    run_info_path.read_text(encoding="utf-8")
                )
                run_info["test_context"] = "live-idle-advisory"
                run_info["gui_state"] = "idle-disarmed"
                _write_json(run_info_path, run_info)
                with self.assertRaisesRegex(
                    ValueError,
                    f"live {kind} requires --evidence-ref",
                ):
                    probe.mark_event(
                        _mark_args(
                            output_dir,
                            kind=kind,
                            operator=operator,
                        )
                    )


class ObserveInjectionTests(unittest.TestCase):
    def _run_observe(
        self,
        args: SimpleNamespace,
        target: FakeTarget,
        *,
        sampler: FakeSampler | None = None,
    ) -> tuple[int, dict]:
        clock = StepClock()
        sleep_count = 0

        def record_markers(_seconds: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count == 2:
                _write_marker(
                    args.output_dir,
                    name="01-precheck",
                    kind="precheck-complete",
                    monotonic_ns=2_100_000_000,
                    at_utc="2026-07-29T12:00:02.100Z",
                    actor="offline-observer",
                )
            elif sleep_count == 3:
                _write_marker(
                    args.output_dir,
                    name="02-close",
                    kind="close-action",
                    monotonic_ns=3_100_000_000,
                    at_utc="2026-07-29T12:00:03.100Z",
                    actor="offline-operator",
                )

        with (
            mock.patch.object(
                probe,
                "current_boot_identity",
                return_value=("boot-test", "offline-test"),
            ),
            mock.patch.object(probe, "_git_value", return_value=""),
        ):
            code = probe.observe(
                args,
                target=target,
                sampler=sampler,
                sleep=record_markers,
                monotonic_ns=clock.monotonic_ns,
                now_utc=clock.now_utc,
            )
        summary = json.loads(
            (args.output_dir / "summary.json").read_text(encoding="utf-8")
        )
        return code, summary

    def test_hwnd_disappears_while_process_remains_alive(self) -> None:
        target = FakeTarget([
            _target_sample(),
            _target_sample(),
            _target_sample(main_exists=False, main_owned=False),
            _target_sample(main_exists=False, main_owned=False),
            _target_sample(main_exists=False, main_owned=False),
        ])
        with tempfile.TemporaryDirectory() as temporary:
            args = _observe_args(
                Path(temporary) / "evidence",
                role="vendor-data-window",
            )
            code, summary = self._run_observe(args, target)
        self.assertEqual(code, probe.EXIT_PASS)
        self.assertIsNotNone(summary["first_main_window_missing"])
        self.assertIsNone(summary["first_process_exit"])
        self.assertEqual(target.close_count, 0)

    def test_owner_change_counts_as_main_window_loss_not_process_exit(self) -> None:
        changed_owner = _target_sample(
            main_exists=True,
            main_owned=False,
            owner_pid=9001,
        )
        target = FakeTarget([
            _target_sample(),
            _target_sample(),
            changed_owner,
            changed_owner,
            changed_owner,
        ])
        with tempfile.TemporaryDirectory() as temporary:
            args = _observe_args(
                Path(temporary) / "evidence",
                role="vendor-data-window",
            )
            code, summary = self._run_observe(args, target)
        self.assertEqual(code, probe.EXIT_PASS)
        event = summary["first_main_window_missing"]["target"]
        self.assertTrue(event["main_window_exists"])
        self.assertFalse(event["main_window_owned_by_target"])
        self.assertEqual(event["main_window"]["owner_pid"], 9001)
        self.assertIsNone(summary["first_process_exit"])

    def test_growth_monitor_main_window_loss_with_live_process_fails(
        self,
    ) -> None:
        lingering = _target_sample(main_exists=False, main_owned=False)
        lingering["owned_top_level_windows"] = [{
            "hwnd": 202,
            "owner_pid": 4242,
            "title": "RHEED Intensity vs Time",
            "class_name": "QtWindow",
            "visible": True,
        }]
        target = FakeTarget([
            _target_sample(),
            _target_sample(),
            lingering,
        ])
        with tempfile.TemporaryDirectory() as temporary:
            args = _observe_args(
                Path(temporary) / "evidence",
                role="growth-monitor",
            )
            args.orphan_grace_s = 2.0
            code, summary = self._run_observe(args, target)
        self.assertEqual(code, probe.EXIT_VALIDATION_FAILURE)
        self.assertIsNotNone(summary["first_main_window_missing"])
        self.assertIsNone(summary["first_process_exit"])

    def test_natural_process_exit_uses_injected_target_sampler_and_clock(
        self,
    ) -> None:
        exited = _target_sample(
            alive=False,
            main_exists=False,
            main_owned=False,
        )
        target = FakeTarget([
            _target_sample(),
            _target_sample(),
            _target_sample(),
            exited,
            exited,
            exited,
        ])
        snapshots = [
            _snapshot(size=10, mtime_ns=1, tail="a", content="a"),
            _snapshot(size=20, mtime_ns=2, tail="b", content="b"),
            _snapshot(size=30, mtime_ns=3, tail="c", content="c"),
            _snapshot(size=30, mtime_ns=3, tail="c", content="c"),
            _snapshot(size=30, mtime_ns=4, tail="c", content="c"),
            _snapshot(size=30, mtime_ns=4, tail="c", content="c"),
        ]
        sampler = FakeSampler(snapshots)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_dir = root / "session"
            session_dir.mkdir()
            (session_dir / "sensor_log.csv").write_text(
                "timestamp,value\n2026-07-29T12:00:00,1\n",
                encoding="utf-8",
            )
            _write_json(
                session_dir / "session_metadata.json",
                {"status": "closed"},
            )
            args = _observe_args(
                root / "evidence",
                role="growth-monitor",
                session_dir=session_dir,
                require_session_advance=["sensor_log.csv"],
            )
            code, summary = self._run_observe(
                args,
                target,
                sampler=sampler,
            )
        self.assertEqual(code, probe.EXIT_PASS)
        self.assertIsNotNone(summary["first_process_exit"])
        self.assertTrue(
            summary["required_session_advances"]["sensor_log.csv"]
        )
        self.assertEqual(summary["late_session_changes"], [])
        self.assertGreaterEqual(sampler.sample_count, 5)
        self.assertEqual(target.close_count, 0)

    def test_sample_failure_seals_action_gate_before_releasing_mutex(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "evidence"
            args = _observe_args(output_dir, role="growth-monitor")
            args.baseline_duration_s = 0.0
            target = FakeTarget([_target_sample()])
            real_build_sample = probe._build_sample
            failure_entered = threading.Event()
            allow_failure = threading.Event()
            call_count = 0

            def failing_build_sample(*call_args, **call_kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 3:
                    failure_entered.set()
                    if not allow_failure.wait(timeout=2):
                        raise RuntimeError("test synchronization timeout")
                    raise RuntimeError("synthetic sample failure")
                return real_build_sample(*call_args, **call_kwargs)

            observer_result: list[int] = []
            marker_result: list[object] = []

            def run_observer() -> None:
                observer_result.append(
                    probe.observe(args, target=target, sleep=lambda _s: None)
                )

            def write_precheck() -> None:
                try:
                    marker_result.append(
                        probe.mark_event(
                            _mark_args(
                                output_dir,
                                kind="precheck-complete",
                                operator="offline-observer",
                            )
                        )
                    )
                except Exception as exc:
                    marker_result.append(exc)

            with (
                mock.patch.object(
                    probe,
                    "_build_sample",
                    side_effect=failing_build_sample,
                ),
                mock.patch.object(probe, "_git_value", return_value=""),
            ):
                observer_thread = threading.Thread(target=run_observer)
                observer_thread.start()
                self.assertTrue(failure_entered.wait(timeout=2))
                marker_thread = threading.Thread(target=write_precheck)
                marker_thread.start()
                allow_failure.set()
                observer_thread.join(timeout=5)
                marker_thread.join(timeout=5)

            self.assertFalse(observer_thread.is_alive())
            self.assertFalse(marker_thread.is_alive())
            self.assertEqual(
                observer_result,
                [probe.EXIT_EVIDENCE_FATAL],
            )
            self.assertEqual(len(marker_result), 1)
            self.assertIsInstance(marker_result[0], RuntimeError)
            status = json.loads(
                (output_dir / "observer_status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(status["sampling_active"])


class SummaryAndMarkerTests(unittest.TestCase):
    def test_late_append_after_process_exit_is_validation_failure(self) -> None:
        late_change = {
            "path": "sensor_log.csv",
            "kind": "appended",
            "before": {"size": 10, "mtime_ns": 1},
            "after": {"size": 20, "mtime_ns": 2},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_dir = root / "pinned-session"
            session_dir.mkdir()
            (session_dir / "sensor_log.csv").write_text(
                "timestamp,value\n2026-07-29T12:00:00,1\n",
                encoding="utf-8",
            )
            output_dir = root / "evidence"
            _summary_fixture(
                output_dir,
                session_dir=str(session_dir),
                late_change=late_change,
            )
            summary, code = probe.summarize_run(output_dir)
        self.assertEqual(code, probe.EXIT_VALIDATION_FAILURE)
        self.assertFalse(summary["session_quiet_ok"])
        self.assertEqual(
            summary["late_session_changes"][0]["changes"][0]["kind"],
            "appended",
        )

    def test_post_run_marker_refreshes_summary_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "evidence"
            _summary_fixture(output_dir)
            _write_json(
                output_dir / "run_complete.json",
                {"lifecycle_errors": []},
            )
            args = SimpleNamespace(
                output_dir=output_dir,
                kind="gui-restarted",
                label="new GUI process observed",
                operator="offline-observer",
                new_pid=5151,
                new_session_dir=Path(temporary) / "new-session",
            )
            code = probe.mark_event(args)
            summary = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )
            marker = next(
                item
                for item in summary["markers"]
                if item["kind"] == "gui-restarted"
            )
            manifest = json.loads(
                (output_dir / "sha256_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(code, probe.EXIT_PASS)
        self.assertTrue(summary["restart_markers"]["gui-restarted"])
        self.assertEqual(marker["new_pid"], 5151)
        self.assertIn("markers/", next(
            name for name in manifest if name.startswith("markers/")
        ))
        self.assertIn("summary.json", manifest)

    def test_gui_restarted_marker_rejects_original_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "evidence"
            _summary_fixture(output_dir)
            args = SimpleNamespace(
                output_dir=output_dir,
                kind="gui-restarted",
                label="incorrect original PID",
                operator="offline-observer",
                new_pid=4242,
                new_session_dir=None,
            )
            with self.assertRaisesRegex(ValueError, "new PID"):
                probe.mark_event(args)
            markers, errors = probe._marker_rows(output_dir)
        self.assertEqual(errors, [])
        self.assertNotIn(
            "gui-restarted",
            {marker["kind"] for marker in markers},
        )

    def test_new_session_marker_rejects_old_session_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_session = root / "old-session"
            old_session.mkdir()
            output_dir = root / "evidence"
            _summary_fixture(
                output_dir,
                session_dir=str(old_session),
            )
            args = SimpleNamespace(
                output_dir=output_dir,
                kind="new-session-started",
                label="incorrect old session reuse",
                operator="offline-operator",
                new_pid=None,
                new_session_dir=old_session / ".." / old_session.name,
            )
            with self.assertRaisesRegex(ValueError, "not reuse"):
                probe.mark_event(args)
            markers, errors = probe._marker_rows(output_dir)
        self.assertEqual(errors, [])
        self.assertNotIn(
            "new-session-started",
            {marker["kind"] for marker in markers},
        )

    def test_finalize_reaudit_detects_old_session_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_dir = root / "old-session"
            session_dir.mkdir()
            sensor_path = session_dir / "sensor_log.csv"
            sensor_path.write_text(
                "timestamp,value\n2026-07-29T12:00:00,1\n",
                encoding="utf-8",
            )
            output_dir = root / "evidence"
            _summary_fixture(
                output_dir,
                session_dir=str(session_dir),
            )
            _write_json(
                output_dir / "session_audit.json",
                probe.audit_session(session_dir),
            )

            with sensor_path.open("a", encoding="utf-8") as stream:
                stream.write("2026-07-29T12:01:00,2\n")

            code = probe.finalize(SimpleNamespace(
                output_dir=output_dir,
                reaudit_session=True,
            ))
            summary = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (output_dir / "sha256_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(code, probe.EXIT_VALIDATION_FAILURE)
        self.assertFalse(summary["session_quiet_ok"])
        change = summary["post_run_old_session_changes"][0]
        self.assertEqual(change["path"], "sensor_log.csv")
        self.assertEqual(change["kind"], "content-changed")
        self.assertIn("session_reaudit.json", manifest)

    def test_marker_refresh_preserves_observer_lifecycle_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "evidence"
            _summary_fixture(output_dir)
            _write_json(
                output_dir / "run_complete.json",
                {"lifecycle_errors": ["target sample failed"]},
            )
            args = SimpleNamespace(
                output_dir=output_dir,
                kind="note",
                label="post-run annotation",
                operator="offline-approver",
                new_pid=None,
                new_session_dir=None,
            )
            code = probe.mark_event(args)
            summary = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )
        self.assertEqual(code, probe.EXIT_EVIDENCE_FATAL)
        self.assertEqual(summary["outcome"], "evidence-fatal")
        self.assertIn("target sample failed", summary["evidence_errors"])

    def test_recovery_chain_reverifies_gui_and_binds_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "evidence"
            evidence = root / "idle-and-arm-evidence.txt"
            evidence.write_text(
                "offline synthetic idle and ARM evidence\n",
                encoding="utf-8",
            )
            evidence_sha256 = probe._sha256(evidence)
            _summary_fixture(
                output_dir,
                require_recovery_validation=True,
            )
            target = _restarted_target()

            restart_code = probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="gui-restarted",
                    operator="offline-observer",
                    new_pid=5151,
                ),
                restart_target=target,
            )
            idle_code = probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="idle-confirmed",
                    operator="offline-observer",
                    evidence_ref=str(evidence),
                ),
                restart_target=target,
            )
            final_code = probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="gui-rearmed",
                    operator="offline-operator",
                    evidence_ref=str(evidence),
                ),
                restart_target=target,
            )
            summary = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(restart_code, probe.EXIT_INCOMPLETE)
        self.assertEqual(idle_code, probe.EXIT_INCOMPLETE)
        self.assertEqual(
            final_code,
            probe.EXIT_PASS,
            summary["recovery_errors"],
        )
        self.assertEqual(summary["recovery_errors"], [])
        recovery = {
            marker["kind"]: marker
            for marker in summary["markers"]
            if marker["kind"] in {
                "gui-restarted",
                "idle-confirmed",
                "gui-rearmed",
            }
        }
        self.assertTrue(
            recovery["idle-confirmed"]["recovery_target_reverified"]
        )
        self.assertTrue(
            recovery["gui-rearmed"]["recovery_target_reverified"]
        )
        self.assertEqual(
            recovery["idle-confirmed"]["evidence_binding"]["sha256"],
            evidence_sha256,
        )

    def test_recovery_step_rejects_a_restarted_gui_that_has_exited(
        self,
    ) -> None:
        alive = _target_sample(owner_pid=5151, hwnd=202)
        exited = _target_sample(
            alive=False,
            main_exists=False,
            main_owned=False,
            owner_pid=5151,
            hwnd=202,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "evidence"
            evidence = root / "idle.txt"
            evidence.write_text("offline evidence\n", encoding="utf-8")
            _summary_fixture(
                output_dir,
                require_recovery_validation=True,
            )
            target = _restarted_target(alive, exited)
            probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="gui-restarted",
                    operator="offline-observer",
                    new_pid=5151,
                ),
                restart_target=target,
            )
            with self.assertRaisesRegex(RuntimeError, "not live"):
                probe.mark_event(
                    _mark_args(
                        output_dir,
                        kind="idle-confirmed",
                        operator="offline-observer",
                        evidence_ref=str(evidence),
                    ),
                    restart_target=target,
                )

    def test_recovery_step_rejects_changed_window_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "evidence"
            evidence = root / "idle.txt"
            evidence.write_text("offline evidence\n", encoding="utf-8")
            _summary_fixture(
                output_dir,
                require_recovery_validation=True,
            )
            target = _restarted_target()
            probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="gui-restarted",
                    operator="offline-observer",
                    new_pid=5151,
                ),
                restart_target=target,
            )
            target.identity = probe.TargetIdentity(
                **{
                    **target.identity.as_dict(),
                    "main_title": "Unexpected Replacement Window",
                }
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "identity field 'main_title' changed",
            ):
                probe.mark_event(
                    _mark_args(
                        output_dir,
                        kind="idle-confirmed",
                        operator="offline-observer",
                        evidence_ref=str(evidence),
                    ),
                    restart_target=target,
                )

    def test_vendor_data_window_can_rebind_on_original_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "evidence"
            evidence = root / "vendor-rebound.txt"
            evidence.write_text("vendor window rebound\n", encoding="utf-8")
            _summary_fixture(
                output_dir,
                role="vendor-data-window",
                require_recovery_validation=True,
            )
            target = _vendor_rebound_target()
            self.assertEqual(
                probe.mark_event(
                    _mark_args(
                        output_dir,
                        kind="gui-restarted",
                        operator="offline-observer",
                        new_pid=4242,
                    ),
                    restart_target=target,
                ),
                probe.EXIT_INCOMPLETE,
            )
            probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="idle-confirmed",
                    operator="offline-observer",
                    evidence_ref=str(evidence),
                ),
                restart_target=target,
            )
            final_code = probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="gui-rearmed",
                    operator="offline-operator",
                    evidence_ref=str(evidence),
                ),
                restart_target=target,
            )
            summary = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(final_code, probe.EXIT_PASS)
        restart = next(
            marker
            for marker in summary["markers"]
            if marker["kind"] == "gui-restarted"
        )
        self.assertEqual(restart["new_target_identity"]["pid"], 4242)
        self.assertEqual(restart["new_target_identity"]["main_hwnd"], 202)

    def test_recovery_evidence_tamper_keeps_run_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "evidence"
            evidence = root / "recovery.txt"
            evidence.write_text("original evidence\n", encoding="utf-8")
            _summary_fixture(
                output_dir,
                require_recovery_validation=True,
            )
            target = _restarted_target()
            probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="gui-restarted",
                    operator="offline-observer",
                    new_pid=5151,
                ),
                restart_target=target,
            )
            probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="idle-confirmed",
                    operator="offline-observer",
                    evidence_ref=str(evidence),
                ),
                restart_target=target,
            )
            self.assertEqual(
                probe.mark_event(
                    _mark_args(
                        output_dir,
                        kind="gui-rearmed",
                        operator="offline-operator",
                        evidence_ref=str(evidence),
                    ),
                    restart_target=target,
                ),
                probe.EXIT_PASS,
            )
            evidence.write_text("tampered evidence\n", encoding="utf-8")
            code = probe.finalize(
                SimpleNamespace(
                    output_dir=output_dir,
                    reaudit_session=False,
                )
            )
            summary = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(code, probe.EXIT_INCOMPLETE)
        self.assertTrue(
            any(
                "evidence verification failed" in error
                for error in summary["recovery_errors"]
            )
        )

    def test_old_session_reaudit_must_follow_recovery_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_session = root / "old-session"
            old_session.mkdir()
            output_dir = root / "evidence"
            evidence = root / "recovery.txt"
            evidence.write_text("recovery evidence\n", encoding="utf-8")
            new_session = root / "new-session"
            new_session.mkdir()
            (new_session / "sensor_log.csv").write_text(
                "timestamp,value\n2026-07-29T12:20:00,1\n",
                encoding="utf-8",
            )
            _summary_fixture(
                output_dir,
                session_dir=str(old_session),
                require_recovery_validation=True,
            )
            probe.reaudit_session(output_dir)
            target = _restarted_target()
            probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="gui-restarted",
                    operator="offline-observer",
                    new_pid=5151,
                ),
                restart_target=target,
            )
            probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="idle-confirmed",
                    operator="offline-observer",
                    evidence_ref=str(evidence),
                ),
                restart_target=target,
            )
            probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="gui-rearmed",
                    operator="offline-operator",
                    evidence_ref=str(evidence),
                ),
                restart_target=target,
            )
            early_code = probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="new-session-started",
                    operator="offline-operator",
                    new_session_dir=new_session,
                    evidence_ref=str(evidence),
                ),
                restart_target=target,
            )
            early_summary = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )
            final_code = probe.finalize(
                SimpleNamespace(
                    output_dir=output_dir,
                    reaudit_session=True,
                )
            )

        self.assertEqual(early_code, probe.EXIT_INCOMPLETE)
        self.assertTrue(
            any(
                "re-audit was not recorded after" in error
                for error in early_summary["recovery_errors"]
            )
        )
        self.assertEqual(final_code, probe.EXIT_PASS)

    def test_new_session_requires_a_complete_sensor_data_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "evidence"
            evidence = root / "recovery.txt"
            evidence.write_text("recovery evidence\n", encoding="utf-8")
            new_session = root / "new-session"
            new_session.mkdir()
            (new_session / "sensor_log.csv").write_text(
                "timestamp,value\n",
                encoding="utf-8",
            )
            _summary_fixture(
                output_dir,
                require_recovery_validation=True,
            )
            target = _restarted_target()
            for kind, operator in (
                ("gui-restarted", "offline-observer"),
                ("idle-confirmed", "offline-observer"),
                ("gui-rearmed", "offline-operator"),
            ):
                probe.mark_event(
                    _mark_args(
                        output_dir,
                        kind=kind,
                        operator=operator,
                        new_pid=5151 if kind == "gui-restarted" else None,
                        evidence_ref=(
                            None
                            if kind == "gui-restarted"
                            else str(evidence)
                        ),
                    ),
                    restart_target=target,
                )
            with self.assertRaisesRegex(
                RuntimeError,
                "at least one flushed data row",
            ):
                probe.mark_event(
                    _mark_args(
                        output_dir,
                        kind="new-session-started",
                        operator="offline-operator",
                        new_session_dir=new_session,
                        evidence_ref=str(evidence),
                    ),
                    restart_target=target,
                )

    def test_recovery_markers_before_target_loss_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "evidence"
            evidence = root / "recovery.txt"
            evidence.write_text("recovery evidence\n", encoding="utf-8")
            _summary_fixture(
                output_dir,
                require_recovery_validation=True,
            )
            target = _restarted_target()
            probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="gui-restarted",
                    operator="offline-observer",
                    new_pid=5151,
                ),
                restart_target=target,
            )
            probe.mark_event(
                _mark_args(
                    output_dir,
                    kind="idle-confirmed",
                    operator="offline-observer",
                    evidence_ref=str(evidence),
                ),
                restart_target=target,
            )
            self.assertEqual(
                probe.mark_event(
                    _mark_args(
                        output_dir,
                        kind="gui-rearmed",
                        operator="offline-operator",
                        evidence_ref=str(evidence),
                    ),
                    restart_target=target,
                ),
                probe.EXIT_PASS,
            )
            forged_times = iter(
                (1_600_000_000, 1_700_000_000, 1_800_000_000)
            )
            for path in sorted((output_dir / "markers").glob("*.json")):
                marker = json.loads(path.read_text(encoding="utf-8"))
                if marker["kind"] in {
                    "gui-restarted",
                    "idle-confirmed",
                    "gui-rearmed",
                }:
                    marker["monotonic_ns"] = next(forged_times)
                    _write_json(path, marker)
            summary, code = probe.summarize_run(output_dir)

        self.assertEqual(code, probe.EXIT_INCOMPLETE)
        self.assertTrue(
            any(
                "did not occur after target loss" in error
                for error in summary["recovery_errors"]
            )
        )


@unittest.skipUnless(
    os.name == "nt",
    "requires a real Windows process HANDLE",
)
class WindowsHeldHandleTests(unittest.TestCase):
    def test_benign_helper_exits_naturally_without_kill(self) -> None:
        """Exercise Win32Target.sample with a held real process handle.

        The helper has no GUI, so this test supplies fake window lookups after
        opening the same limited query/wait handle used by Win32Target.  The
        child exits on its own; the test never calls terminate(), kill(), or a
        window API that changes state.
        """
        import ctypes
        import ctypes.wintypes

        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(0.75)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        target = probe.Win32Target.__new__(probe.Win32Target)
        target._ctypes = ctypes
        target._wintypes = ctypes.wintypes
        target._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        target._user32 = ctypes.WinDLL("user32", use_last_error=True)
        target._configure_api()
        access = (
            probe.Win32Target.PROCESS_QUERY_LIMITED_INFORMATION
            | probe.Win32Target.SYNCHRONIZE
        )
        target._handle = target._kernel32.OpenProcess(
            access,
            False,
            child.pid,
        )
        self.assertTrue(target._handle)
        target._pid = child.pid
        target.identity = probe.TargetIdentity(
            pid=child.pid,
            creation_time_utc="not-needed-for-wait-test",
            image_path=sys.executable,
            command_line=f"{sys.executable} -c natural-exit-helper",
            command_line_sha256="not-needed-for-wait-test",
            command_line_source="offline-test",
            main_hwnd=101,
            main_title="offline helper",
            main_class_name="offline",
            requested_process_access=access,
        )
        target._window_info = lambda _hwnd: None
        target._enum_owned_windows = lambda: []
        try:
            self.assertTrue(target.sample()["process_alive"])
            self.assertEqual(child.wait(timeout=5), 0)
            exited = target.sample()
            self.assertFalse(exited["process_alive"])
            self.assertEqual(exited["exit_code"], 0)
        finally:
            target.close()
            # Natural-exit cleanup only.  Deliberately no kill/terminate.
            child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
