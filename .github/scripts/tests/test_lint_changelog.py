"""Tests for .github/scripts/lint_changelog.py (FND-4274)."""

from __future__ import annotations

from pathlib import Path

import pytest
from lint_changelog import lint, main

_REPO_ROOT = Path(__file__).resolve().parents[3]

GOOD = """# Changelog

## v3.61.0 (November 27, 2026)

Full Changelog: https://github.com/o/r/compare/v3.60.0...v3.61.0

### Features

- add a thing [FND-1] (#5200) (by @dev in [abc1234](https://github.com/o/r/commit/abc1234))

### Bug Fixes

- **BREAKING:** fix a thing (#5201) (by @dev in [def5678](https://github.com/o/r/commit/def5678))

## v3.60.0 (November 26, 2026)

- older (#5100) (by @dev in [aaa1111](https://github.com/o/r/commit/aaa1111))
"""


def test_clean_section_passes() -> None:
    assert lint(GOOD, "3.61.0") == []
    assert lint(GOOD, "v3.61.0") == []


def test_missing_section() -> None:
    (problem,) = lint(GOOD, "3.62.0")
    assert "no `## v3.62.0" in problem


def test_duplicate_heading() -> None:
    text = GOOD + "\n## v3.61.0 (November 20, 2026)\n\n- dup (#1)\n"
    problems = lint(text, "3.61.0")
    assert len(problems) == 1
    assert "appears 2 times (lines 3, 19)" in problems[0]


def test_empty_section() -> None:
    text = GOOD.replace(
        "## v3.60.0",
        "## v3.62.0 (November 28, 2026)\n\nFull Changelog: x\n\n## v3.60.0",
    )
    (problem,) = lint(text, "3.62.0")
    assert "has no bullets" in problem


def test_bullet_without_pr_reference() -> None:
    text = GOOD.replace("fix a thing (#5201)", "fix a thing")
    (problem,) = lint(text, "3.61.0")
    assert "no `(#N)` PR reference" in problem
    assert "fix a thing" in problem


@pytest.mark.parametrize("ref", ["#5201", "(5201)", "[#5201]", "PR 5201"])
def test_only_the_squash_merge_form_counts(ref: str) -> None:
    text = GOOD.replace("(#5201)", ref)
    assert len(lint(text, "3.61.0")) == 1


def test_older_sections_are_not_linted() -> None:
    text = GOOD.replace("older (#5100)", "older")
    assert lint(text, "3.61.0") == []


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(GOOD, encoding="utf-8")
    assert main(["3.61.0", "--changelog", str(path)]) == 0
    assert main(["3.62.0", "--changelog", str(path)]) == 1
    assert "::error file=" in capsys.readouterr().out


def test_sdk_release_workflow_lints_before_pushing() -> None:
    """The lint must run after the changelog is generated and before the push."""
    text = (_REPO_ROOT / ".github/workflows/release.yaml").read_text(encoding="utf-8")
    generate = text.index("update_changelog.py")
    lint_step = text.index("lint_changelog.py")
    push = text.index("Commit and push bump branch")
    assert generate < lint_step < push
