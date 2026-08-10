"""Read Growth Monitor session archives without assuming contiguous sampling."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class FrameRecord:
    frame_index: int
    heartbeat_idx: int
    elapsed_s: float
    captured_at_utc: str
    capture_sequence: int
    temperature_c: float
    frame_name: str
    member: str
    capture_backend: str
    capture_geometry_id: str
    frame_sha256: str = ""

    def provenance(self) -> dict[str, object]:
        return {
            "frame_index": self.frame_index,
            "heartbeat_idx": self.heartbeat_idx,
            "elapsed_s": self.elapsed_s,
            "captured_at_utc": self.captured_at_utc,
            "capture_sequence": self.capture_sequence,
            "frame_name": self.frame_name,
            "frame_sha256": self.frame_sha256,
        }


@dataclass(frozen=True)
class SessionArchive:
    path: Path
    sha256: str
    heartbeat_member: str
    metadata_member: str
    metadata: dict[str, object]
    frames: tuple[FrameRecord, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _member(archive: zipfile.ZipFile, suffix: str) -> str:
    matches = [name for name in archive.namelist() if name.replace("\\", "/").endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(f"Expected one ZIP member ending in {suffix!r}; found {len(matches)}")
    return matches[0]


def load_session_archive(path: str | Path) -> SessionArchive:
    source = Path(path).resolve()
    if not source.is_file() or not zipfile.is_zipfile(source):
        raise ValueError(f"Not a readable session ZIP: {source}")
    archive_sha = sha256_file(source)
    with zipfile.ZipFile(source) as archive:
        heartbeat_member = _member(archive, "heartbeat_log.csv")
        metadata_member = _member(archive, "session_metadata.json")
        metadata = json.loads(archive.read(metadata_member).decode("utf-8-sig"))
        raw_rows = list(csv.DictReader(io.StringIO(archive.read(heartbeat_member).decode("utf-8-sig"))))
        root = PurePosixPath(heartbeat_member.replace("\\", "/")).parent
        names = set(archive.namelist())
        frames: list[FrameRecord] = []
        seen: set[str] = set()
        for raw in raw_rows:
            frame_path = str(raw.get("frame_path", "")).strip()
            if not frame_path:
                continue
            frame_name = frame_path.replace("\\", "/").rsplit("/", 1)[-1]
            member = str(root / "frames" / frame_name)
            if member not in names:
                raise ValueError(f"Heartbeat frame is absent from ZIP: {member}")
            if member in seen:
                raise ValueError(f"Duplicate heartbeat frame reference: {member}")
            seen.add(member)
            try:
                elapsed = float(raw["elapsed_s"])
                heartbeat = int(raw["heartbeat_idx"])
                sequence = int(raw["capture_sequence"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Invalid heartbeat provenance") from exc
            if not math.isfinite(elapsed):
                raise ValueError("elapsed_s must be finite")
            temperature_text = str(raw.get("pyrometer_temp_C", "")).strip()
            temperature = float(temperature_text) if temperature_text else math.nan
            if temperature_text and not math.isfinite(temperature):
                raise ValueError("pyrometer_temp_C must be finite when present")
            captured_at_utc = str(raw.get("captured_at_utc", "")).strip()
            try:
                parsed_utc = datetime.fromisoformat(
                    captured_at_utc[:-1] + "+00:00" if captured_at_utc.endswith("Z") else captured_at_utc
                )
            except ValueError as exc:
                raise ValueError("captured_at_utc is invalid") from exc
            if parsed_utc.tzinfo is None:
                raise ValueError("captured_at_utc must include a timezone")
            frames.append(FrameRecord(
                frame_index=len(frames), heartbeat_idx=heartbeat, elapsed_s=elapsed,
                captured_at_utc=captured_at_utc,
                capture_sequence=sequence, temperature_c=temperature,
                frame_name=frame_name, member=member,
                capture_backend=str(raw.get("capture_backend", metadata.get("camera_backend", ""))),
                capture_geometry_id=str(raw.get("capture_geometry_id", metadata.get("capture_geometry_id", metadata.get("geometry", "")))),
            ))
    if not frames:
        raise ValueError("Session contains no saved heartbeat frames")
    if any(b.elapsed_s <= a.elapsed_s for a, b in zip(frames, frames[1:])):
        raise ValueError("Saved-frame elapsed_s must be strictly increasing")
    if any(b.heartbeat_idx <= a.heartbeat_idx for a, b in zip(frames, frames[1:])):
        raise ValueError("Saved-frame heartbeat_idx must be strictly increasing")
    if any(b.capture_sequence <= a.capture_sequence for a, b in zip(frames, frames[1:])):
        raise ValueError("Saved-frame capture_sequence must be strictly increasing")
    capture_times = [
        datetime.fromisoformat(frame.captured_at_utc[:-1] + "+00:00" if frame.captured_at_utc.endswith("Z") else frame.captured_at_utc).astimezone(timezone.utc)
        for frame in frames
    ]
    if any(b <= a for a, b in zip(capture_times, capture_times[1:])):
        raise ValueError("Saved-frame captured_at_utc must be strictly increasing")
    return SessionArchive(source, archive_sha, heartbeat_member, metadata_member, metadata, tuple(frames))


def hash_frame_payloads(session: SessionArchive) -> SessionArchive:
    frames: list[FrameRecord] = []
    with zipfile.ZipFile(session.path) as archive:
        for frame in session.frames:
            digest = hashlib.sha256(archive.read(frame.member)).hexdigest()
            frames.append(replace(frame, frame_sha256=digest))
    return replace(session, frames=tuple(frames))


def ordered_frame_fingerprint(frames: tuple[FrameRecord, ...]) -> str:
    digest = hashlib.sha256()
    for frame in frames:
        digest.update((json.dumps(frame.provenance(), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"
