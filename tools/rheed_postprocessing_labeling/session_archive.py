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
    root_member: str = ""
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class CsvMember:
    """A CSV inside the immutable source archive plus tamper evidence."""

    member: str
    sha256: str
    rows: tuple[dict[str, str], ...]


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


def optional_member(session: SessionArchive, suffix: str) -> str | None:
    """Return the unique member ending in *suffix*, or ``None`` when absent."""

    normalized = suffix.replace("\\", "/")
    matches = [name for name in session.members if name.replace("\\", "/").endswith(normalized)]
    if len(matches) > 1:
        raise ValueError(f"Expected at most one ZIP member ending in {suffix!r}; found {len(matches)}")
    return matches[0] if matches else None


def read_csv_member(session: SessionArchive, suffix: str) -> CsvMember | None:
    """Read an optional archived CSV without changing the source archive."""

    member = optional_member(session, suffix)
    if member is None:
        return None
    with zipfile.ZipFile(session.path) as archive:
        payload = archive.read(member)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Archived CSV is not UTF-8: {member}") from exc
    rows = tuple(
        {str(key): "" if value is None else str(value) for key, value in row.items()}
        for row in csv.DictReader(io.StringIO(text, newline=""))
    )
    return CsvMember(member, hashlib.sha256(payload).hexdigest(), rows)


def resolve_archived_path(session: SessionArchive, value: str) -> str | None:
    """Resolve a logged Windows/relative path to one unambiguous ZIP member."""

    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return None
    lowered = raw.lower()
    exact = [name for name in session.members if name.replace("\\", "/").lower() == lowered]
    if len(exact) == 1:
        return exact[0]
    # Session logs commonly contain an absolute workstation path.  Match the
    # longest available suffix first, then fall back to the basename only when
    # it is unique.  Ambiguity fails closed instead of choosing a frame.
    parts = PurePosixPath(raw).parts
    for width in range(min(len(parts), 5), 0, -1):
        suffix = "/".join(parts[-width:]).lower()
        matches = [
            name for name in session.members
            if name.replace("\\", "/").lower().endswith(suffix)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and width == 1:
            return None
    return None


def read_archived_bytes(session: SessionArchive, member: str) -> bytes:
    """Read an exact raw ZIP member for provenance-sensitive processing."""

    if member not in session.members:
        raise ValueError(f"Unknown ZIP member: {member}")
    with zipfile.ZipFile(session.path) as archive:
        return archive.read(member)


def read_raw_frame(session: SessionArchive, frame_index: int) -> bytes:
    """Return the original BMP/PNG bytes for a zero-based saved-frame index."""

    if frame_index < 0 or frame_index >= len(session.frames):
        raise IndexError("frame index is outside the saved-frame sequence")
    return read_archived_bytes(session, session.frames[frame_index].member)


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
    root_member = str(PurePosixPath(heartbeat_member.replace("\\", "/")).parent)
    return SessionArchive(
        source, archive_sha, heartbeat_member, metadata_member, metadata,
        tuple(frames), root_member, tuple(sorted(names)),
    )


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
