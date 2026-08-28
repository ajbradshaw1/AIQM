#!/usr/bin/env python3
"""Offline regression tests for model-weight integrity and local recovery."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_model_weights as fetcher  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(
    rel_path: str,
    data: bytes,
    role: str = "shadow_runtime",
) -> dict[str, object]:
    return {
        "path": rel_path,
        "sha256": _sha(data),
        "size_bytes": len(data),
        "role": role,
    }


def _manifest(entries: list[dict[str, object]]) -> dict[str, object]:
    role_counts = {
        role: sum(entry["role"] == role for entry in entries)
        for role in fetcher.WEIGHT_ROLES
    }
    role_bytes = {
        role: sum(
            int(entry["size_bytes"])
            for entry in entries
            if entry["role"] == role
        )
        for role in fetcher.WEIGHT_ROLES
    }
    return {
        "schema_version": 1,
        "roles": {
            role: f"Test description for {role}"
            for role in fetcher.WEIGHT_ROLES
        },
        "totals": {
            "files": len(entries),
            "bytes": sum(int(entry["size_bytes"]) for entry in entries),
            "shadow_runtime_files": role_counts["shadow_runtime"],
            "shadow_runtime_bytes": role_bytes["shadow_runtime"],
        },
        "files": entries,
    }


@dataclass
class WeightTree:
    repo: Path
    source: Path

    def write_source(self, rel_path: str, data: bytes) -> Path:
        path = self.source.joinpath(*Path(rel_path).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_target(self, rel_path: str, data: bytes) -> Path:
        path = self.repo.joinpath(*Path(rel_path).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def target(self, rel_path: str) -> Path:
        return self.repo.joinpath(*Path(rel_path).parts)


@pytest.fixture
def weight_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WeightTree:
    repo = tmp_path / "repo"
    source = tmp_path / "approved-source"
    (repo / "models").mkdir(parents=True)
    source.mkdir()
    monkeypatch.setattr(fetcher, "REPO_ROOT", repo)
    monkeypatch.setattr(fetcher, "MODELS_ROOT", repo / "models")
    monkeypatch.setattr(
        fetcher,
        "MANIFEST_PATH",
        repo / "models" / "WEIGHTS_MANIFEST.json",
    )
    return WeightTree(repo=repo, source=source)


REL = "models/pkg/model.pth"
GOOD = b"0123456789abcdef"
BAD = b"fedcba9876543210"


def test_successful_recovery_is_verified(weight_tree: WeightTree) -> None:
    weight_tree.write_source(REL, GOOD)

    assert fetcher.do_fetch([_entry(REL, GOOD)], weight_tree.source) == 0
    assert weight_tree.target(REL).read_bytes() == GOOD
    assert fetcher.verify_one(_entry(REL, GOOD)) == (True, "ok")


def test_missing_source_fails_without_creating_target(
    weight_tree: WeightTree,
) -> None:
    assert fetcher.do_fetch([_entry(REL, GOOD)], weight_tree.source) == 1
    assert not weight_tree.target(REL).exists()


def test_wrong_source_digest_is_refused_and_target_is_preserved(
    weight_tree: WeightTree,
) -> None:
    weight_tree.write_source(REL, BAD)
    weight_tree.write_target(REL, BAD)

    assert fetcher.do_fetch([_entry(REL, GOOD)], weight_tree.source) == 1
    assert weight_tree.target(REL).read_bytes() == BAD


def test_interrupted_copy_preserves_target_and_removes_partial(
    weight_tree: WeightTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weight_tree.write_source(REL, GOOD)
    weight_tree.write_target(REL, BAD)

    def exploding_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated interrupted copy")

    monkeypatch.setattr(shutil, "copy2", exploding_copy)

    assert fetcher.do_fetch([_entry(REL, GOOD)], weight_tree.source) == 1
    assert weight_tree.target(REL).read_bytes() == BAD
    parent = weight_tree.target(REL).parent
    assert not [
        path for path in parent.iterdir() if path.name.endswith(".partial")
    ]


def test_already_correct_target_is_always_skipped(
    weight_tree: WeightTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weight_tree.write_target(REL, GOOD)

    def unexpected_copy(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an already-correct target was recopied")

    monkeypatch.setattr(shutil, "copy2", unexpected_copy)

    assert fetcher.do_fetch([_entry(REL, GOOD)], weight_tree.source) == 0
    assert weight_tree.target(REL).read_bytes() == GOOD


@pytest.mark.parametrize(
    "hostile",
    [
        "models/../../escape.pth",
        "/etc/passwd.pth",
        "C:/Windows/file.pth",
        r"models\..\escape.pth",
        "other/model.pth",
        "models/model.bin",
        "models//model.pth",
        "models/./model.pth",
    ],
)
def test_manifest_path_is_constrained_to_canonical_model_weights(
    weight_tree: WeightTree,
    hostile: str,
) -> None:
    with pytest.raises(SystemExit):
        fetcher.resolve_entry_path(hostile)


def test_source_symlink_escape_is_rejected(
    weight_tree: WeightTree,
) -> None:
    external = weight_tree.source.parent / "external"
    external.mkdir()
    linked = weight_tree.source / "models"
    try:
        linked.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(SystemExit, match="escapes the explicit source"):
        fetcher.resolve_source_entry_path(weight_tree.source, REL)


def test_role_filter_uses_shadow_only_language() -> None:
    manifest = {
        "files": [
            _entry("models/a.pth", b"a", "shadow_runtime"),
            _entry("models/b.pth", b"b", "benchmark"),
            _entry("models/c.pth", b"c", "shadow_runtime"),
        ]
    }

    assert len(fetcher.select(manifest, "shadow_runtime")) == 2
    assert len(fetcher.select(manifest, "benchmark")) == 1
    assert len(fetcher.select(manifest, "all")) == 3
    with pytest.raises(SystemExit):
        fetcher.select(manifest, "deployed")


def test_verify_reports_missing_wrong_size_and_wrong_hash(
    weight_tree: WeightTree,
) -> None:
    entries = [
        _entry("models/present.pth", GOOD),
        _entry("models/missing.pth", GOOD),
        _entry("models/corrupt.pth", GOOD),
        _entry("models/truncated.pth", GOOD),
    ]
    weight_tree.write_target("models/present.pth", GOOD)
    weight_tree.write_target("models/corrupt.pth", BAD)
    weight_tree.write_target("models/truncated.pth", GOOD[:5])

    assert fetcher.verify_one(entries[0]) == (True, "ok")
    assert fetcher.verify_one(entries[1])[1] == "missing"
    assert fetcher.verify_one(entries[2])[1] == "sha256 mismatch"
    assert "wrong size" in fetcher.verify_one(entries[3])[1]
    assert fetcher.do_verify(entries) == 1


def test_manifest_validation_rejects_legacy_deployed_role(
    weight_tree: WeightTree,
) -> None:
    manifest = {
        "schema_version": 1,
        "files": [_entry(REL, GOOD, role="deployed")],
    }
    with pytest.raises(SystemExit, match="Invalid role"):
        fetcher._validate_manifest(manifest)


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("files", 999),
        ("bytes", 999),
        ("shadow_runtime_files", 999),
        ("shadow_runtime_bytes", 999),
    ],
)
def test_manifest_validation_rejects_totals_mismatch(
    field: str,
    wrong_value: int,
) -> None:
    manifest = _manifest([
        _entry("models/shadow.pth", b"shadow", "shadow_runtime"),
        _entry("models/benchmark.pth", b"benchmark", "benchmark"),
    ])
    manifest["totals"][field] = wrong_value

    with pytest.raises(SystemExit, match=rf"totals\.{field} mismatch"):
        fetcher._validate_manifest(manifest)


@pytest.mark.parametrize("missing_role", fetcher.WEIGHT_ROLES)
def test_manifest_validation_rejects_an_empty_declared_role(
    missing_role: str,
) -> None:
    retained_role = next(
        role for role in fetcher.WEIGHT_ROLES if role != missing_role
    )
    manifest = _manifest([
        _entry("models/only.pth", b"only", retained_role),
    ])

    with pytest.raises(
        SystemExit,
        match=rf"role {missing_role!r} must contain at least one file",
    ):
        fetcher._validate_manifest(manifest)


def test_manifest_validation_rejects_incomplete_role_declarations() -> None:
    manifest = _manifest([
        _entry("models/shadow.pth", b"shadow", "shadow_runtime"),
        _entry("models/benchmark.pth", b"benchmark", "benchmark"),
    ])
    del manifest["roles"]["benchmark"]

    with pytest.raises(SystemExit, match="roles must declare exactly"):
        fetcher._validate_manifest(manifest)


def test_empty_role_selection_fails_closed() -> None:
    manifest = {
        "files": [_entry("models/benchmark.pth", b"benchmark", "benchmark")],
    }

    with pytest.raises(SystemExit, match="No model-weight files selected"):
        fetcher.select(manifest, "shadow_runtime")


def test_cli_defaults_to_verification_without_a_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(REL, GOOD)
    seen: list[list[dict[str, object]]] = []
    monkeypatch.setattr(
        fetcher,
        "load_manifest",
        lambda: {"schema_version": 1, "files": [entry]},
    )
    monkeypatch.setattr(
        fetcher,
        "do_verify",
        lambda entries: seen.append(entries) or 0,
    )

    def unexpected_fetch(
        _entries: list[dict[str, object]],
        _source: Path,
    ) -> int:
        raise AssertionError("CLI inferred a recovery source")

    monkeypatch.setattr(fetcher, "do_fetch", unexpected_fetch)

    assert fetcher.main([]) == 0
    assert seen == [[entry]]


def test_cli_recovers_only_from_explicit_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "reviewed-source"
    source.mkdir()
    entry = _entry(REL, GOOD)
    seen: list[Path] = []
    monkeypatch.setattr(
        fetcher,
        "load_manifest",
        lambda: {"schema_version": 1, "files": [entry]},
    )
    monkeypatch.setattr(
        fetcher,
        "do_fetch",
        lambda _entries, explicit_source: seen.append(explicit_source) or 0,
    )

    assert fetcher.main(["--source", str(source)]) == 0
    assert seen == [source]


def test_verify_only_rejects_a_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(SystemExit):
        fetcher.main(["--verify-only", "--source", str(source)])


def test_shipped_manifest_matches_all_74_tracked_weights() -> None:
    manifest = fetcher.load_manifest()
    entries = fetcher.select(manifest, "all")

    assert len(entries) == 74
    assert {entry["role"] for entry in entries} == {
        "shadow_runtime",
        "benchmark",
    }
    assert sum(entry["role"] == "shadow_runtime" for entry in entries) == 37
    assert sum(entry["role"] == "benchmark" for entry in entries) == 37
    assert "deployed" not in manifest["roles"]
    assert "weak_shadow_only" in manifest["roles"]["shadow_runtime"]

    manifest_paths = {entry["path"] for entry in entries}
    actual_paths = {
        path.relative_to(fetcher.REPO_ROOT).as_posix()
        for path in fetcher.MODELS_ROOT.rglob("*.pth")
    }
    assert manifest_paths == actual_paths

    failures = [
        (entry["path"], fetcher.verify_one(entry)[1])
        for entry in entries
        if not fetcher.verify_one(entry)[0]
    ]
    assert failures == []


def test_manifest_json_is_stable_and_has_expected_totals() -> None:
    manifest_path = (
        Path(__file__).resolve().parent.parent
        / "models"
        / "WEIGHTS_MANIFEST.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["files"]

    assert manifest["totals"] == {
        "files": 74,
        "bytes": 212477710,
        "shadow_runtime_files": 37,
        "shadow_runtime_bytes": 106237487,
    }
    assert sum(entry["size_bytes"] for entry in entries) == 212477710
