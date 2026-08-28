import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_README = REPO_ROOT / "tools" / "rheed_postprocessing_labeling" / "README.md"


def test_tool_readme_describes_current_point_event_contract() -> None:
    text = TOOL_README.read_text(encoding="utf-8")
    prose = re.sub(r"\s+", " ", text)

    required_phrases = (
        "rheed-point-events-v3",
        "`manual`",
        "`auto_capture`",
        "`posthoc`",
        "read-only reference points",
        "read-only context, not label fields",
        "**Unfinished**",
        "explicitly click **Complete**",
        "Comments are optional",
        "Good or Bad",
        "representative **Anchor**",
        "`127.0.0.1`",
        "original saved-frame bytes",
        "Legacy `rheed-temporal-segments-v1`",
        "read-only compatibility data",
    )
    for phrase in required_phrases:
        assert phrase in prose

    assert "## Label temporal segments" not in text
    assert "use **Mark In** and **Mark Out**" not in text


def test_repository_and_ci_discover_the_offline_point_event_tool() -> None:
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    pytest_config = (REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )

    assert "tools/rheed_postprocessing_labeling/" in root_readme
    assert "legacy interval annotations as read-only" in root_readme
    assert "tools/rheed_postprocessing_labeling/tests" in pytest_config
    assert "scripts" not in {
        line.strip() for line in pytest_config.splitlines() if not line.lstrip().startswith("#")
    }
    assert "run: python -m pytest -q" in workflow
