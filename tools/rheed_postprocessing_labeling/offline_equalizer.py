"""On-demand, exact-frame Equalizer controller for the offline point labeler.

Only the desktop labeler imports this module.  Static reports can still edit
Draft annotations, but they have no authority to read the source ZIP, accept a
camera calibration, or mark an event Complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Callable, Mapping

import numpy as np
from PIL import Image
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QDialog, QLabel, QMessageBox, QVBoxLayout

from gui.equalizer_alignment import (
    BasisBundle,
    CalibrationRecord,
    RheedFrameSnapshot,
    calibration_is_stale,
)
from gui.equalizer_label_contract import (
    ACTIVE_LABELS,
    frame_rgb_sha256,
    validate_equalizer_payload,
)
from gui.live_equalizer_tab import LiveEqualizerTab

from .loopback_service import EqualizerRequest, LoopbackReportService
from .report_builder import load_report_payload
from .session_archive import (
    SessionArchive,
    load_session_archive,
    optional_member,
    read_archived_bytes,
    read_raw_frame,
    resolve_archived_path,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class OfflineFrameContext:
    event_id: str
    reviewer: str
    review_frame_index: int
    raw_frame_sha256: str
    rgb: np.ndarray
    metadata: dict[str, object]


class OfflineCalibrationJournal:
    """Small append-only sidecar for calibrations accepted after acquisition."""

    def __init__(self, report_root: Path) -> None:
        self.root = report_root.resolve() / "annotations"
        self.frames = self.root / "frames"
        self.path = self.root / "equalizer_calibrations.jsonl"
        self.transaction = self.root / ".equalizer_calibration.transaction.json"
        self.untrusted = self.root / ".equalizer_calibrations.untrusted"

    @staticmethod
    def _event_line(event: Mapping[str, object]) -> bytes:
        return json.dumps(
            event, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8") + b"\n"

    def _recover_transaction(self) -> None:
        """Finish a committed append or roll back an uncommitted evidence file."""
        if not self.transaction.exists():
            return
        try:
            wal = json.loads(self.transaction.read_text(encoding="utf-8"))
            if not isinstance(wal, dict) or wal.get("schema_version") != 1:
                raise ValueError("transaction schema is invalid")
            event = wal["event"]
            if not isinstance(event, dict):
                raise ValueError("transaction event is invalid")
            line = self._event_line(event)
            if hashlib.sha256(line).hexdigest() != wal.get("event_sha256"):
                raise ValueError("transaction event hash is invalid")
            journal = self.path.read_bytes() if self.path.exists() else b""
            committed = journal.endswith(line)
            if not committed and journal:
                # A partial JSON line cannot be repaired by guessing how much
                # reached the filesystem.  Preserve all evidence and fail
                # closed for an operator-assisted audit.
                if not journal.endswith(b"\n"):
                    raise ValueError("journal ends with an interrupted line")
                for raw in journal.splitlines():
                    json.loads(raw)
            evidence_text = str(wal.get("evidence_path") or "")
            evidence = self.root / evidence_text if evidence_text else None
            evidence_hash = str(wal.get("evidence_sha256") or "")
            if committed:
                if evidence is not None:
                    if not evidence.is_file():
                        raise ValueError("committed orientation evidence is missing")
                    if hashlib.sha256(evidence.read_bytes()).hexdigest() != evidence_hash:
                        raise ValueError("committed orientation evidence hash differs")
            elif evidence is not None and evidence.exists():
                if hashlib.sha256(evidence.read_bytes()).hexdigest() != evidence_hash:
                    raise ValueError("uncommitted orientation evidence hash differs")
                evidence.unlink()
            self.transaction.unlink()
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            _atomic_write(
                self.untrusted,
                f"{_utc_now()} transaction recovery failed: {exc}\n".encode("utf-8"),
            )
            raise OSError(f"Equalizer transaction recovery failed closed: {exc}") from exc

    def _append(
        self,
        event: Mapping[str, object],
        *,
        evidence_path: str = "",
        evidence_sha256: str = "",
    ) -> None:
        if self.untrusted.exists():
            raise OSError(
                "Equalizer sidecar is marked untrusted; repair it before continuing"
            )
        self._recover_transaction()
        self.root.mkdir(parents=True, exist_ok=True)
        line = self._event_line(event)
        _atomic_write(self.transaction, json.dumps({
            "schema_version": 1,
            "event_sha256": hashlib.sha256(line).hexdigest(),
            "event": event,
            "evidence_path": evidence_path,
            "evidence_sha256": evidence_sha256,
            "started_at_utc": _utc_now(),
        }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        try:
            with self.path.open("ab") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            self.transaction.unlink()
        except BaseException:
            _atomic_write(
                self.untrusted,
                f"{_utc_now()} interrupted Equalizer journal append\n".encode("utf-8"),
            )
            raise

    def read_active(self) -> dict[str, CalibrationRecord]:
        if self.untrusted.exists():
            raise OSError("Equalizer sidecar is marked untrusted")
        self._recover_transaction()
        if not self.path.exists():
            return {}
        active: dict[str, CalibrationRecord] = {}
        manifests: set[str] = set()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            for number, raw in enumerate(lines, start=1):
                if not raw.strip():
                    continue
                event = json.loads(raw)
                if not isinstance(event, dict) or event.get("journal_schema_version") != 1:
                    raise ValueError(f"invalid journal schema at line {number}")
                calibration = CalibrationRecord.from_json_dict(event["calibration"])
                event_type = event.get("event")
                if event_type == "accepted":
                    manifest = event.get("basis_bundle_manifest")
                    if manifest is not None:
                        validated = BasisBundle.validate_manifest_dict(manifest)
                        manifests.add(str(validated["bundle_id"]))
                    if calibration.basis_bundle_id not in manifests:
                        raise ValueError(f"missing basis manifest at line {number}")
                    evidence = self.root / calibration.orientation_evidence_path
                    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
                    if digest != calibration.orientation_evidence_sha256:
                        raise ValueError(f"orientation evidence mismatch at line {number}")
                    if calibration.calibration_id in active:
                        raise ValueError(f"duplicate calibration at line {number}")
                    active[calibration.calibration_id] = calibration
                elif event_type == "invalidated":
                    prior = active.get(calibration.calibration_id)
                    if prior is None:
                        raise ValueError(f"unknown invalidation at line {number}")
                    active.pop(calibration.calibration_id)
                else:
                    raise ValueError(f"unknown journal event at line {number}")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            _atomic_write(
                self.untrusted,
                f"{_utc_now()} {exc}\n".encode("utf-8"),
            )
            raise OSError(f"Equalizer sidecar failed closed: {exc}") from exc
        return active

    def accept(
        self,
        calibration: CalibrationRecord,
        snapshot: RheedFrameSnapshot,
        bundle: BasisBundle,
    ) -> None:
        if not calibration.grower_accepted or calibration.invalidated_reason:
            raise ValueError("Only an accepted active calibration may be journaled")
        if calibration.basis_bundle_id != bundle.bundle_id:
            raise ValueError("Calibration and basis bundle differ")
        evidence = snapshot.orientation_evidence_png()
        if hashlib.sha256(evidence).hexdigest() != calibration.orientation_evidence_sha256:
            raise ValueError("Orientation evidence hash does not match calibration")
        relative = Path(calibration.orientation_evidence_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Orientation evidence path is unsafe")
        destination = self.root / relative
        if destination.exists():
            raise FileExistsError("Orientation evidence already exists")
        _atomic_write(destination, evidence)
        event = {
            "journal_schema_version": 1,
            "event": "accepted",
            "recorded_at_utc": _utc_now(),
            "calibration": calibration.to_json_dict(),
            "orientation_evidence_path": calibration.orientation_evidence_path,
            "basis_bundle_manifest": bundle.to_manifest_dict(),
        }
        try:
            self._append(
                event,
                evidence_path=calibration.orientation_evidence_path,
                evidence_sha256=calibration.orientation_evidence_sha256,
            )
        except BaseException:
            try:
                destination.unlink()
            except OSError:
                pass
            raise

    def invalidate(self, calibration: CalibrationRecord, reason: str) -> None:
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("Calibration invalidation requires a reason")
        self._append({
            "journal_schema_version": 1,
            "event": "invalidated",
            "recorded_at_utc": _utc_now(),
            "calibration": calibration.invalidated(reason).to_json_dict(),
        })


def read_archived_calibrations(
    session: SessionArchive,
) -> dict[str, CalibrationRecord]:
    """Replay trusted acquisition-time calibrations without modifying the ZIP.

    A malformed line, missing basis manifest, unsafe/missing evidence image, or
    hash mismatch rejects the whole archived journal.  The caller may then
    create a fresh sidecar calibration, but it must never silently reuse a
    partially trusted acquisition calibration.
    """

    member = optional_member(session, "equalizer_calibrations.jsonl")
    if member is None:
        return {}
    try:
        text = read_archived_bytes(session, member).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Archived Equalizer calibration journal is not UTF-8") from exc
    active: dict[str, CalibrationRecord] = {}
    accepted: dict[str, CalibrationRecord] = {}
    manifests: dict[str, dict[str, object]] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
            if not isinstance(event, dict) or event.get("journal_schema_version") != 1:
                raise ValueError("unsupported journal schema")
            calibration = CalibrationRecord.from_json_dict(event["calibration"])
            event_type = event.get("event")
            if event_type == "accepted":
                manifest_value = event.get("basis_bundle_manifest")
                if manifest_value is not None:
                    manifest = BasisBundle.validate_manifest_dict(manifest_value)
                    bundle_id = str(manifest["bundle_id"])
                    if bundle_id in manifests:
                        raise ValueError("duplicate basis bundle manifest")
                    manifests[bundle_id] = manifest
                if calibration.basis_bundle_id not in manifests:
                    raise ValueError("calibration references an unrecorded basis bundle")
                if (
                    not calibration.grower_accepted
                    or calibration.invalidated_reason
                    or calibration.calibration_id in accepted
                ):
                    raise ValueError("invalid or duplicate accepted calibration")
                evidence_member = resolve_archived_path(
                    session, calibration.orientation_evidence_path,
                )
                if evidence_member is None:
                    raise ValueError("calibration orientation evidence is absent or ambiguous")
                digest = hashlib.sha256(
                    read_archived_bytes(session, evidence_member),
                ).hexdigest()
                if digest != calibration.orientation_evidence_sha256.lower():
                    raise ValueError("calibration orientation evidence hash differs")
                accepted[calibration.calibration_id] = calibration
                active[calibration.calibration_id] = calibration
            elif event_type == "invalidated":
                prior = accepted.get(calibration.calibration_id)
                if (
                    prior is None
                    or not calibration.invalidated_reason
                    or calibration.grower_accepted
                    or prior.invalidated(calibration.invalidated_reason).to_json_dict()
                    != calibration.to_json_dict()
                ):
                    raise ValueError("calibration invalidation does not match acceptance")
                active.pop(calibration.calibration_id, None)
            else:
                raise ValueError("unknown calibration journal event")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Archived Equalizer calibration journal failed at line {number}: {exc}"
            ) from exc
    return active


def _event_ids(config: Mapping[str, object]) -> set[str]:
    container = config.get("point_events")
    if isinstance(container, Mapping):
        raw_events = container.get("events")
    elif isinstance(container, list):
        raw_events = container
    else:
        raw_events = config.get("events")
    if not isinstance(raw_events, list):
        return set()
    return {
        str(item.get("event_id") or "")
        for item in raw_events
        if isinstance(item, Mapping) and item.get("event_id")
    }


def _frame_contexts(config: Mapping[str, object]) -> list[object]:
    point = config.get("point_events")
    if isinstance(point, Mapping) and isinstance(point.get("frame_contexts"), list):
        return list(point["frame_contexts"])
    if isinstance(config.get("frame_contexts"), list):
        return list(config["frame_contexts"])
    return []


def resolve_offline_frame(
    report_path: Path,
    session: SessionArchive,
    request: EqualizerRequest,
    *,
    additional_event_ids: set[str] | None = None,
) -> OfflineFrameContext:
    """Bind an event request to one exact raw archive frame, or fail closed."""

    payload = load_report_payload(report_path)
    config = payload["config"]
    if not isinstance(config, dict):
        raise ValueError("Report config is invalid")
    dataset = config.get("dataset")
    if not isinstance(dataset, dict):
        raise ValueError("Report has no dataset provenance")
    if dataset.get("source_archive_sha256") != session.sha256:
        raise ValueError("Selected session ZIP does not match this report")
    known_ids = _event_ids(config) | set(additional_event_ids or ())
    if request.event_id not in known_ids:
        raise ValueError("Selected event is not present in this report or its sidecar")
    index = request.review_frame_index - 1
    count = int(config.get("count", 0))
    if index < 0 or index >= count or index >= len(session.frames):
        raise ValueError("Review anchor is outside the saved-frame sequence")
    frame = session.frames[index]
    raw = read_raw_frame(session, index)
    raw_hash = hashlib.sha256(raw).hexdigest()
    hashes = payload.get("frame_sha256")
    if not isinstance(hashes, list) or index >= len(hashes) or hashes[index] != raw_hash:
        raise ValueError("Raw archive frame hash does not match the report")
    with Image.open(io.BytesIO(raw)) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()

    contexts = _frame_contexts(config)
    context = contexts[index] if index < len(contexts) else None
    if not isinstance(context, Mapping):
        raise ValueError(
            "This saved frame lacks the stable camera/view provenance required by Equalizer"
        )
    required = {
        "received_monotonic_ns", "source_hwnd", "view_segment_id",
        "visual_history_generation", "gun_aligned", "realignment_active",
    }
    missing = sorted(key for key in required if context.get(key) in (None, ""))
    if missing:
        raise ValueError("Equalizer provenance is missing: " + ", ".join(missing))
    if context.get("gun_aligned") is not True or context.get("realignment_active") is not False:
        raise ValueError("Selected frame was not captured in a stable aligned RHEED view")
    received = int(context["received_monotonic_ns"])
    if received <= 0:
        raise ValueError("Equalizer requires a positive captured monotonic timestamp")
    metadata = {
        "captured_at_utc": frame.captured_at_utc,
        "received_monotonic_ns": received,
        "capture_sequence": frame.capture_sequence,
        "source_hwnd": int(context["source_hwnd"]),
        "capture_backend": frame.capture_backend,
        "capture_geometry_id": frame.capture_geometry_id,
        "camera_width": int(context.get("camera_width") or rgb.shape[1]),
        "camera_height": int(context.get("camera_height") or rgb.shape[0]),
        "frame_age_ms": float(context.get("frame_age_ms") or 0.0),
        "view_segment_id": int(context["view_segment_id"]),
        "visual_history_generation": int(context["visual_history_generation"]),
        "gun_aligned": True,
        "realignment_active": False,
        "session_id": str(
            context.get("session_id")
            or session.metadata.get("session_id")
            or session.path.stem
        ),
    }
    if metadata["camera_width"] != rgb.shape[1] or metadata["camera_height"] != rgb.shape[0]:
        raise ValueError("Raw frame dimensions do not match archived camera provenance")
    return OfflineFrameContext(
        event_id=request.event_id,
        reviewer=request.reviewer,
        review_frame_index=request.review_frame_index,
        raw_frame_sha256=raw_hash,
        rgb=rgb,
        metadata=metadata,
    )


class OfflineEqualizerCoordinator(QObject):
    """Queue HTTP requests onto Qt and own one retrospective dialog at a time."""

    request_received = pyqtSignal(object)
    resolution_finished = pyqtSignal(object, object, str)

    def __init__(
        self,
        *,
        report_path: Path,
        session_path: Path,
        service: LoopbackReportService,
        additional_event_ids: Callable[[], set[str]] | None = None,
        session_loader: Callable[[], SessionArchive] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.report_path = report_path.resolve()
        self.session_path = session_path.resolve()
        self.session: SessionArchive | None = None
        self.service = service
        self.additional_event_ids = additional_event_ids or (lambda: set())
        self.session_loader = session_loader
        self.journal = OfflineCalibrationJournal(self.report_path.parent)
        self._dialog: QDialog | None = None
        self._active_request: EqualizerRequest | None = None
        self._active_context: OfflineFrameContext | None = None
        self._resolving_request: EqualizerRequest | None = None
        self.request_received.connect(self._open_request)
        self.resolution_finished.connect(self._finish_resolution)

    def enqueue_from_http(self, request: EqualizerRequest) -> None:
        self.request_received.emit(request)

    @pyqtSlot(object)
    def _open_request(self, request: object) -> None:
        if not isinstance(request, EqualizerRequest):
            return
        if self._dialog is not None or self._resolving_request is not None:
            self.service.fail_equalizer(
                request.request_id,
                "Finish or close the currently open Equalizer before starting another.",
            )
            return
        self._resolving_request = request
        thread = threading.Thread(
            target=self._resolve_background,
            args=(request,),
            name="rheed-offline-frame-resolver",
            daemon=True,
        )
        thread.start()

    def _resolve_background(self, request: EqualizerRequest) -> None:
        try:
            session = self.session
            if session is None:
                session = (
                    self.session_loader()
                    if self.session_loader is not None
                    else load_session_archive(self.session_path)
                )
            context = resolve_offline_frame(
                self.report_path,
                session,
                request,
                additional_event_ids=self.additional_event_ids(),
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self.resolution_finished.emit(request, None, str(exc))
            return
        self.session = session
        self.resolution_finished.emit(request, context, "")

    @pyqtSlot(object, object, str)
    def _finish_resolution(
        self,
        request: object,
        context: object,
        error: str,
    ) -> None:
        if not isinstance(request, EqualizerRequest):
            return
        if (
            self._resolving_request is None
            or self._resolving_request.request_id != request.request_id
        ):
            self.service.fail_equalizer(request.request_id, "Equalizer request was superseded")
            return
        self._resolving_request = None
        if error or not isinstance(context, OfflineFrameContext):
            self.service.fail_equalizer(request.request_id, error or "Frame resolution failed")
            return
        try:
            dialog, panel, status = self._build_dialog(request, context)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self.service.fail_equalizer(request.request_id, str(exc))
            return
        self._dialog = dialog
        self._active_request = request
        self._active_context = context
        dialog.finished.connect(lambda _code: self._dialog_closed(request.request_id))
        panel.calibration_accept_requested.connect(
            lambda pending: self._accept_calibration(panel, status, pending)
        )
        panel.calibration_invalidation_requested.connect(
            lambda reason: self._invalidate_calibration(panel, status, str(reason))
        )
        panel.live_label_save_requested.connect(
            lambda payload: self._save_measurement(panel, status, payload)
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def close(self) -> None:
        """Close UI state; the owning launcher stops the HTTP service."""
        if self._dialog is not None:
            self._dialog.close()
        resolving = self._resolving_request
        if resolving is not None:
            self.service.fail_equalizer(
                resolving.request_id,
                "Desktop labeler closed while the source frame was being verified.",
            )
        self._resolving_request = None

    def _build_dialog(
        self,
        request: EqualizerRequest,
        context: OfflineFrameContext,
    ) -> tuple[QDialog, LiveEqualizerTab, QLabel]:
        metadata = context.metadata
        dialog = QDialog()
        dialog.setWindowTitle(
            f"Event {request.event_id} Equalizer - saved frame {request.review_frame_index}"
        )
        dialog.resize(1180, 820)
        layout = QVBoxLayout(dialog)
        status = QLabel(
            "Equalizer fits visual simulator bases to the exact archived frame. "
            "It never sets the human reconstruction label or a model probability."
        )
        status.setWordWrap(True)
        layout.addWidget(status)
        panel = LiveEqualizerTab(dialog, retrospective=True)
        layout.addWidget(panel, 1)
        panel.set_session_id(str(metadata["session_id"]))
        panel.update_qc_context(
            session_active=True,
            view_segment_id=int(metadata["view_segment_id"]),
            visual_history_generation=int(metadata["visual_history_generation"]),
            gun_aligned=True,
            realignment_active=False,
        )
        panel.set_save_enabled(True)
        panel.update_camera_frame(context.rgb, metadata)
        bundle = panel.get_basis_bundle()
        snapshot = panel.get_current_snapshot()
        if bundle is None or snapshot is None:
            raise ValueError("Canonical basis or exact frozen frame could not be loaded")
        compatible: CalibrationRecord | None = None
        candidates = list(self.journal.read_active().values())
        if self.session is not None:
            archived = read_archived_calibrations(self.session)
            candidates.extend(
                calibration
                for calibration_id, calibration in archived.items()
                if calibration_id not in {
                    item.calibration_id for item in candidates
                }
            )
        for candidate in candidates:
            stale, _reason = calibration_is_stale(
                candidate,
                source_hwnd=snapshot.source_hwnd,
                camera_width=snapshot.camera_width,
                camera_height=snapshot.camera_height,
                capture_backend=snapshot.capture_backend,
                capture_geometry_id=snapshot.capture_geometry_id,
                view_segment_id=snapshot.view_segment_id,
                visual_history_generation=snapshot.visual_history_generation,
                session_id=snapshot.session_id,
                basis_bundle_id=bundle.bundle_id,
                gun_aligned=True,
                realignment_active=False,
                session_active=True,
            )
            if not stale and candidate.grower_accepted:
                compatible = candidate
                break
        if compatible is not None and panel.set_accepted_calibration(compatible):
            dialog._offline_active_calibration = compatible
            status.setText(
                f"Reusing compatible accepted calibration {compatible.calibration_id}. "
                "Review the fit before saving."
            )
        else:
            dialog._offline_active_calibration = None
            status.setText(
                "No compatible accepted calibration is available. Use Calibrate, "
                "review the three-point normal/mirrored overlay, confirm handedness, "
                "and Accept before saving."
            )
        dialog._offline_equalizer_panel = panel
        return dialog, panel, status

    def _accept_calibration(
        self,
        panel: LiveEqualizerTab,
        status: QLabel,
        pending: object,
    ) -> None:
        context = self._active_context
        if context is None or not isinstance(pending, CalibrationRecord):
            return
        snapshot = panel.get_calibration_snapshot()
        bundle = panel.get_basis_bundle()
        evidence_kind = panel.get_handedness_evidence_kind()
        if snapshot is None or bundle is None or not evidence_kind:
            status.setText("Calibration blocked: confirm asymmetric handedness evidence.")
            return
        try:
            accepted = pending.accepted(
                accepted_by=context.reviewer,
                orientation_evidence_sha256=snapshot.orientation_evidence_sha256(),
                orientation_evidence_kind=evidence_kind,
            )
            self.journal.accept(accepted, snapshot, bundle)
            if not panel.set_accepted_calibration(accepted):
                raise ValueError("Equalizer rejected the journaled calibration")
        except (OSError, TypeError, ValueError) as exc:
            panel.invalidate_calibration(f"acceptance failed: {exc}", emit=False)
            status.setText(f"Calibration was not accepted: {exc}")
            return
        if self._dialog is not None:
            self._dialog._offline_active_calibration = accepted
        status.setText(
            f"Accepted calibration {accepted.calibration_id}; it is bound to "
            "the archived camera geometry and basis bundle."
        )

    def _invalidate_calibration(
        self,
        panel: LiveEqualizerTab,
        status: QLabel,
        reason: str,
    ) -> None:
        dialog = self._dialog
        calibration = (
            None if dialog is None else getattr(dialog, "_offline_active_calibration", None)
        )
        if not isinstance(calibration, CalibrationRecord):
            return
        try:
            self.journal.invalidate(calibration, reason)
        except (OSError, TypeError, ValueError) as exc:
            status.setText(f"Calibration invalidation could not be journaled: {exc}")
            panel.set_accepted_calibration(calibration)
            return
        dialog._offline_active_calibration = None
        status.setText("Calibration invalidated and journaled. Recalibrate before saving.")

    def _save_measurement(
        self,
        panel: LiveEqualizerTab,
        status: QLabel,
        payload: object,
    ) -> None:
        request = self._active_request
        context = self._active_context
        calibration = panel.get_calibration()
        snapshot = panel.get_current_snapshot()
        bundle = panel.get_basis_bundle()
        if (
            request is None
            or context is None
            or not isinstance(calibration, CalibrationRecord)
            or snapshot is None
            or bundle is None
        ):
            status.setText("Equalizer save blocked: accepted calibration is missing.")
            return
        try:
            validated = validate_equalizer_payload(payload if isinstance(payload, Mapping) else {})
            if calibration.basis_bundle_id != bundle.bundle_id:
                raise ValueError("Calibration basis bundle changed")
            if frame_rgb_sha256(context.rgb) != frame_rgb_sha256(snapshot.rgb):
                raise ValueError("Frozen Equalizer frame no longer matches the archived frame")
            for weights in ("raw_weights", "final_weights", "normalized_weights"):
                if validated[weights].get("HTR") is not None:
                    raise ValueError("HTR Equalizer basis must remain unavailable")
            measurement = {
                "schema_version": 1,
                "measurement_type": "equalizer_visual_basis_fit",
                "valid": True,
                "measured_at_utc": _utc_now(),
                "event_id": context.event_id,
                "review_frame_index": context.review_frame_index,
                "frame_sha256": context.raw_frame_sha256,
                "frame_sha256_algorithm": "raw-file-bytes-v1",
                "frame_rgb_sha256": frame_rgb_sha256(context.rgb),
                "capture_sequence": int(context.metadata["capture_sequence"]),
                "calibration_id": calibration.calibration_id,
                "basis_bundle_id": bundle.bundle_id,
                "active_classes": list(ACTIVE_LABELS),
                "unavailable_classes": ["HTR"],
                "weights": {
                    "raw": validated["raw_weights"],
                    "final": validated["final_weights"],
                    "normalized": validated["normalized_weights"],
                },
                "HTR": None,
                "fit_mode": validated["fit_mode"],
                "normalization_applied": validated["normalization_applied"],
                "fit_residual": validated["residual_rms"],
                "valid_coverage": validated["valid_coverage"],
                "confidence": validated["confidence"],
                "calibration": {
                    "matrix": calibration.matrix.tolist(),
                    "parity": calibration.parity,
                    "endpoint_order": calibration.endpoint_order,
                    "rotation_deg": calibration.rotation_deg,
                    "scale": calibration.scale,
                    "rms_residual_px": calibration.rms_residual_px,
                    "max_residual_px": calibration.max_residual_px,
                    "valid_coverage": calibration.valid_coverage,
                    "orientation_evidence_kind": calibration.orientation_evidence_kind,
                },
                "reviewer": context.reviewer,
                "scientific_meaning": (
                    "visual_basis_fit_not_human_label_not_model_probability"
                ),
            }
            self.service.complete_equalizer(request.request_id, measurement)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            status.setText(f"Equalizer measurement was not saved: {exc}")
            return
        status.setText(
            "Equalizer measurement returned to the selected event. Review the "
            "comment and human interpretation, then explicitly click Complete."
        )
        if self._dialog is not None:
            self._dialog.accept()

    def _dialog_closed(self, request_id: str) -> None:
        try:
            status = self.service.equalizer_status(request_id)
        except KeyError:
            status = {}
        if status.get("status") == "pending":
            self.service.fail_equalizer(request_id, "Equalizer closed without saving a measurement")
        self._dialog = None
        self._active_request = None
        self._active_context = None


__all__ = [
    "OfflineCalibrationJournal",
    "OfflineEqualizerCoordinator",
    "OfflineFrameContext",
    "read_archived_calibrations",
    "resolve_offline_frame",
]
