"""Locate the Classifier2 source repository without machine-local guessing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence


AI_REPO_ROOT_ENV = "AI_REPO_ROOT"
INSTRUMENT_REPO_ROOT = Path(__file__).resolve().parents[1]

# Standalone lab installations that predate the multi-repository workspace.
LEGACY_AI_REPO_ROOTS: tuple[Path, ...] = (
    Path(r"C:\Users\Lab10\AI_for_quantum"),
    Path(r"C:\Users\Omicron\AI_for_quantum"),
    Path("/Users/aj/ai-for-quantum"),
)

_CLASSIFIER2_LAYOUTS: tuple[Path, ...] = (
    Path("src") / "classifiers" / "classifier2",
    Path("Classifier2"),
)


def classifier2_directory(root: str | Path) -> Path:
    """Select a complete Classifier2 layout, preferring the nested layout."""
    repository = Path(root).expanduser()
    for relative in _CLASSIFIER2_LAYOUTS:
        candidate = repository / relative
        if (candidate / "evaluate.py").is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        f"Classifier repository {repository} does not contain "
        "src/classifiers/classifier2/evaluate.py or Classifier2/evaluate.py."
    )


def _has_classifier2_layout(root: Path) -> bool:
    """Return whether *root* contains a supported Classifier2 checkout."""
    try:
        classifier2_directory(root)
    except FileNotFoundError:
        return False
    return True


def _layout_candidates(instrument_root: Path) -> tuple[Path, ...]:
    """Build candidates for standalone, sibling-repo, and workspace layouts."""
    candidates = [
        instrument_root,
        instrument_root.parent / "rheed-perception",
        instrument_root.parent / "RHEEDClassify",
    ]
    for ancestor in instrument_root.parents:
        # Linked worktrees can be several levels below the workspace root.
        # Require the orchestration lock before consulting an ancestor's
        # repos/ tree so an unrelated C:\repos or /repos cannot be selected.
        if (ancestor / "workspace" / "repos.lock.yaml").is_file():
            candidates.extend(
                (
                    ancestor / "repos" / "rheed-perception",
                    ancestor / "repos" / "RHEEDClassify",
                )
            )
    return tuple(candidates)


def _unique_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        expanded = path.expanduser()
        key = os.path.normcase(os.path.abspath(expanded))
        if key not in seen:
            seen.add(key)
            unique.append(expanded)
    return tuple(unique)


def resolve_ai_repo_root(
    *,
    environ: Mapping[str, str] | None = None,
    instrument_root: str | Path | None = None,
    legacy_roots: Sequence[str | Path] = LEGACY_AI_REPO_ROOTS,
) -> Path:
    """Resolve the repository that owns Classifier2.

    ``AI_REPO_ROOT`` is authoritative: an invalid explicit value raises
    immediately instead of silently selecting a different checkout. Without
    an override, migrated/sibling layouts are tried before legacy lab paths.
    """
    environment = os.environ if environ is None else environ
    if AI_REPO_ROOT_ENV in environment:
        override = environment[AI_REPO_ROOT_ENV].strip()
        if not override:
            raise FileNotFoundError(
                f"{AI_REPO_ROOT_ENV} is set but empty. Remove it to enable "
                "automatic discovery, or set it to a Classifier2 repository."
            )
        override_path = Path(override).expanduser()
        if _has_classifier2_layout(override_path):
            return override_path.resolve()
        raise FileNotFoundError(
            f"{AI_REPO_ROOT_ENV} points to {override_path}, but that directory "
            "does not contain Classifier2/evaluate.py or "
            "src/classifiers/classifier2/evaluate.py."
        )

    root = Path(instrument_root or INSTRUMENT_REPO_ROOT).expanduser().resolve()
    candidates = _unique_paths(
        (*_layout_candidates(root), *(Path(path) for path in legacy_roots))
    )
    for candidate in candidates:
        if _has_classifier2_layout(candidate):
            return candidate.resolve()

    checked = "\n  - ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        "Could not locate a Classifier2 repository. Checked:\n"
        f"  - {checked}\n"
        f"Set {AI_REPO_ROOT_ENV} to a repository containing Classifier2."
    )
