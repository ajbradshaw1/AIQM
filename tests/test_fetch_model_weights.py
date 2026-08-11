#!/usr/bin/env python3
"""Tests for the model-weight fetcher.

This is installation-critical code: it is the only path by which a lab
machine gets the 203 MB of weights that used to be committed. A silent
failure here produces a GUI that either will not start or — worse — runs
against a truncated checkpoint.

Everything is synthetic. No real weight is read, and the fixtures are a few
hundred bytes, so the whole file runs in well under a second.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import fetch_model_weights as fetcher  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(rel: str, data: bytes, role: str = "deployed") -> dict:
    return {
        "path": rel, "sha256": _sha(data),
        "size_bytes": len(data), "role": role,
    }


class _Fixture:
    """Redirects the module's repo-root constants at a temporary tree.

    The fetcher resolves everything against REPO_ROOT so it cannot write
    outside models/; pointing those at a temp dir keeps the tests honest
    about that logic while never touching the real repository.
    """

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.source = tmp / "source"
        (self.repo / "models").mkdir(parents=True)
        self.source.mkdir()
        self._saved = (
            fetcher.REPO_ROOT, fetcher.MODELS_ROOT, fetcher.MANIFEST_PATH,
        )
        fetcher.REPO_ROOT = self.repo
        fetcher.MODELS_ROOT = self.repo / "models"
        fetcher.MANIFEST_PATH = self.repo / "models" / "WEIGHTS_MANIFEST.json"

    def restore(self) -> None:
        (fetcher.REPO_ROOT, fetcher.MODELS_ROOT,
         fetcher.MANIFEST_PATH) = self._saved

    def write_source(self, rel: str, data: bytes) -> None:
        path = self.source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def write_target(self, rel: str, data: bytes) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def target_bytes(self, rel: str) -> bytes:
        return (self.repo / rel).read_bytes()


def _with_fixture(body):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _Fixture(Path(tmp))
        try:
            body(fixture)
        finally:
            fixture.restore()


REL = "models/pkg/model.pth"
GOOD = b"good-weights-payload"
BAD = b"different-payload-xx"


def test_successful_fetch_copies_and_verifies() -> None:
    def body(fx: _Fixture) -> None:
        fx.write_source(REL, GOOD)
        rc = fetcher.do_fetch([_entry(REL, GOOD)], fx.source, force=False)
        assert rc == 0
        assert fx.target_bytes(REL) == GOOD
    _with_fixture(body)


def test_missing_source_file_fails_without_writing() -> None:
    def body(fx: _Fixture) -> None:
        rc = fetcher.do_fetch([_entry(REL, GOOD)], fx.source, force=False)
        assert rc == 1
        assert not (fx.repo / REL).exists(), "wrote a target with no source"
    _with_fixture(body)


def test_wrong_source_digest_is_refused_before_writing() -> None:
    """A bad source must never land, not even to be deleted afterwards."""
    def body(fx: _Fixture) -> None:
        fx.write_source(REL, BAD)
        rc = fetcher.do_fetch([_entry(REL, GOOD)], fx.source, force=False)
        assert rc == 1
        assert not (fx.repo / REL).exists(), (
            "a mismatched file was written into the tree"
        )
    _with_fixture(body)


def test_failed_copy_preserves_an_existing_valid_target() -> None:
    """The whole reason for the temp-file dance.

    A source that changed between the pre-check and the copy — or a copy
    interrupted partway — must not destroy a target that was already
    correct. Simulated by making the copy itself raise after the source
    passed its digest check.
    """
    def body(fx: _Fixture) -> None:
        fx.write_source(REL, GOOD)
        fx.write_target(REL, GOOD)
        original = shutil.copy2

        def exploding_copy(src, dst, *args, **kwargs):
            raise OSError("simulated interruption mid-copy")

        shutil.copy2 = exploding_copy
        try:
            rc = fetcher.do_fetch([_entry(REL, GOOD)], fx.source, force=True)
        finally:
            shutil.copy2 = original

        assert rc == 1, "an interrupted copy reported success"
        assert fx.target_bytes(REL) == GOOD, (
            "the previously valid target was destroyed by a failed copy"
        )
        leftovers = list((fx.repo / "models" / "pkg").glob("*.partial"))
        assert not leftovers, f"temporary files left behind: {leftovers}"
    _with_fixture(body)


def test_path_traversal_is_rejected() -> None:
    """A manifest is exactly the file someone hand-edits in a hurry."""
    def body(fx: _Fixture) -> None:
        for hostile in ("models/../../escape.pth", "/etc/passwd",
                        "models/pkg/../../../outside.pth"):
            try:
                fetcher.resolve_entry_path(hostile)
            except SystemExit:
                continue
            raise AssertionError(f"path escaped models/: {hostile!r}")
        # A legitimate path still resolves. Both sides are resolved
        # because macOS puts temp dirs behind the /var -> /private/var
        # symlink, which resolve() follows and a raw path does not.
        assert fetcher.resolve_entry_path(REL).is_relative_to(
            (fx.repo / "models").resolve()
        )
    _with_fixture(body)


def test_role_filter_selects_the_right_subset() -> None:
    manifest = {
        "files": [
            _entry("models/a.pth", b"a", "deployed"),
            _entry("models/b.pth", b"b", "benchmark"),
            _entry("models/c.pth", b"c", "deployed"),
        ],
    }
    assert len(fetcher.select(manifest, "deployed")) == 2
    assert len(fetcher.select(manifest, "benchmark")) == 1
    assert len(fetcher.select(manifest, "all")) == 3


def test_verify_only_reports_each_failure_kind() -> None:
    def body(fx: _Fixture) -> None:
        entries = [
            _entry("models/present.pth", GOOD),
            _entry("models/missing.pth", GOOD),
            _entry("models/corrupt.pth", GOOD),
            _entry("models/truncated.pth", GOOD),
        ]
        fx.write_target("models/present.pth", GOOD)
        fx.write_target("models/corrupt.pth", BAD)          # right size, wrong bytes
        fx.write_target("models/truncated.pth", GOOD[:5])   # wrong size

        assert fetcher.verify_one(entries[0]) == (True, "ok")
        assert fetcher.verify_one(entries[1])[1] == "missing"
        assert fetcher.verify_one(entries[2])[1] == "sha256 mismatch"
        assert "wrong size" in fetcher.verify_one(entries[3])[1]
        assert fetcher.do_verify(entries) == 1
    _with_fixture(body)


def test_already_correct_files_are_not_recopied() -> None:
    """Re-copying 83 MB to reach identical bytes is only a chance to lose it."""
    def body(fx: _Fixture) -> None:
        fx.write_source(REL, GOOD)
        fx.write_target(REL, GOOD)
        shutil_copy_calls = []
        original = shutil.copy2

        def counting_copy(src, dst, *args, **kwargs):
            shutil_copy_calls.append(src)
            return original(src, dst, *args, **kwargs)

        shutil.copy2 = counting_copy
        try:
            rc = fetcher.do_fetch([_entry(REL, GOOD)], fx.source, force=False)
        finally:
            shutil.copy2 = original
        assert rc == 0
        assert not shutil_copy_calls, "recopied a file that already verified"
    _with_fixture(body)


def test_the_real_manifest_is_well_formed() -> None:
    """The shipped manifest must satisfy the script's own constraints."""
    repo_root = Path(__file__).resolve().parent.parent
    manifest = json.loads(
        (repo_root / "models" / "WEIGHTS_MANIFEST.json").read_text()
    )
    assert manifest["schema_version"] == 1
    files = manifest["files"]
    assert len(files) == 74, len(files)
    assert len({e["path"] for e in files}) == len(files), "duplicate paths"
    for entry in files:
        assert entry["role"] in ("deployed", "benchmark"), entry
        assert len(entry["sha256"]) == 64, entry
        assert entry["size_bytes"] > 0, entry
        assert not Path(entry["path"]).is_absolute(), entry
        assert ".." not in Path(entry["path"]).parts, entry
        assert entry["path"].startswith("models/"), entry
    deployed = [e for e in files if e["role"] == "deployed"]
    assert len(deployed) == 37, len(deployed)


TESTS = [
    value for name, value in sorted(globals().items())
    if name.startswith("test_") and callable(value)
]


def main() -> int:
    failures = []
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
