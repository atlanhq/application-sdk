"""Tests for .github/scripts/guard_release_files.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import guard_release_files as grf

# ---------------------------------------------------------------------------
# Sample diffs
# ---------------------------------------------------------------------------


def _diff(*hunks: str) -> str:
    return "\n".join(hunks) + "\n"


CONFORMANCE_VERSION_DIFF = _diff(
    "diff --git a/packages/conformance/pyproject.toml b/packages/conformance/pyproject.toml",
    "index 111..222 100644",
    "--- a/packages/conformance/pyproject.toml",
    "+++ b/packages/conformance/pyproject.toml",
    "@@ -5,7 +5,7 @@",
    ' name = "atlan-application-sdk-conformance"',
    '-version = "0.15.0"',
    '+version = "0.16.0"',
    ' description = "..."',
)

CONFORMANCE_DUNDER_DIFF = _diff(
    "diff --git a/packages/conformance/conformance/__init__.py b/packages/conformance/conformance/__init__.py",
    "--- a/packages/conformance/conformance/__init__.py",
    "+++ b/packages/conformance/conformance/__init__.py",
    "@@ -7 +7 @@",
    '-__version__ = "0.15.0"',
    '+__version__ = "0.16.0"',
)

CONFORMANCE_CHANGELOG_DIFF = _diff(
    "diff --git a/packages/conformance/CHANGELOG.md b/packages/conformance/CHANGELOG.md",
    "--- a/packages/conformance/CHANGELOG.md",
    "+++ b/packages/conformance/CHANGELOG.md",
    "@@ -3,0 +4,3 @@",
    "+## [Unreleased]",
    "+",
    "+- hand-written note",
)

TOOLKIT_VERSION_DIFF = _diff(
    "diff --git a/contract-toolkit/src/PklProject b/contract-toolkit/src/PklProject",
    "--- a/contract-toolkit/src/PklProject",
    "+++ b/contract-toolkit/src/PklProject",
    "@@ -8 +8 @@",
    '-  version = "0.19.0"',
    '+  version = "0.20.0"',
)

TOOLKIT_CHANGELOG_DIFF = _diff(
    "diff --git a/contract-toolkit/CHANGELOG.md b/contract-toolkit/CHANGELOG.md",
    "--- a/contract-toolkit/CHANGELOG.md",
    "+++ b/contract-toolkit/CHANGELOG.md",
    "@@ -1 +1,2 @@",
    "+manual edit",
)

# A dependency bump to the conformance pyproject that leaves the version line
# untouched must NOT trip the guard.
CONFORMANCE_DEP_ONLY_DIFF = _diff(
    "diff --git a/packages/conformance/pyproject.toml b/packages/conformance/pyproject.toml",
    "--- a/packages/conformance/pyproject.toml",
    "+++ b/packages/conformance/pyproject.toml",
    "@@ -20,7 +20,7 @@",
    " dependencies = [",
    '-    "jinja2>=3.1.0",',
    '+    "jinja2>=3.1.5",',
    " ]",
)

UNRELATED_DIFF = _diff(
    "diff --git a/application_sdk/foo.py b/application_sdk/foo.py",
    "--- a/application_sdk/foo.py",
    "+++ b/application_sdk/foo.py",
    "@@ -1 +1 @@",
    "-x = 1",
    "+x = 2",
)

# The SDK's own version is deliberately not guarded here.
SDK_VERSION_DIFF = _diff(
    "diff --git a/pyproject.toml b/pyproject.toml",
    "--- a/pyproject.toml",
    "+++ b/pyproject.toml",
    "@@ -3 +3 @@",
    '-version = "3.24.0"',
    '+version = "3.25.0"',
)


# ---------------------------------------------------------------------------
# parse_diff
# ---------------------------------------------------------------------------


class TestParseDiff:
    def test_captures_changed_lines_without_prefix(self):
        parsed = grf.parse_diff(CONFORMANCE_VERSION_DIFF)
        path = "packages/conformance/pyproject.toml"
        assert path in parsed
        assert 'version = "0.15.0"' in parsed[path]
        assert 'version = "0.16.0"' in parsed[path]

    def test_skips_file_header_lines(self):
        parsed = grf.parse_diff(CONFORMANCE_VERSION_DIFF)
        lines = parsed["packages/conformance/pyproject.toml"]
        assert not any(line.startswith("++ ") for line in lines)
        assert not any("a/packages/conformance" in line for line in lines)

    def test_touched_file_is_a_key_even_without_content(self):
        parsed = grf.parse_diff(
            _diff(
                "diff --git a/packages/conformance/CHANGELOG.md b/packages/conformance/CHANGELOG.md",
                "old mode 100644",
                "new mode 100755",
            )
        )
        assert "packages/conformance/CHANGELOG.md" in parsed

    def test_multiple_files(self):
        parsed = grf.parse_diff(CONFORMANCE_VERSION_DIFF + CONFORMANCE_CHANGELOG_DIFF)
        assert "packages/conformance/pyproject.toml" in parsed
        assert "packages/conformance/CHANGELOG.md" in parsed


# ---------------------------------------------------------------------------
# evaluate -- violations off the bump branch
# ---------------------------------------------------------------------------


class TestViolationsOffBumpBranch:
    @pytest.mark.parametrize(
        "diff,expected_path",
        [
            (CONFORMANCE_VERSION_DIFF, "packages/conformance/pyproject.toml"),
            (
                CONFORMANCE_DUNDER_DIFF,
                "packages/conformance/conformance/__init__.py",
            ),
            (CONFORMANCE_CHANGELOG_DIFF, "packages/conformance/CHANGELOG.md"),
            (TOOLKIT_VERSION_DIFF, "contract-toolkit/src/PklProject"),
            (TOOLKIT_CHANGELOG_DIFF, "contract-toolkit/CHANGELOG.md"),
        ],
    )
    def test_flags_release_owned_edits(self, diff, expected_path):
        violations = grf.evaluate(grf.parse_diff(diff), "feature/whatever")
        assert expected_path in [p for p, _ in violations]

    def test_version_and_changelog_together(self):
        parsed = grf.parse_diff(CONFORMANCE_VERSION_DIFF + CONFORMANCE_CHANGELOG_DIFF)
        paths = [p for p, _ in grf.evaluate(parsed, "feature/x")]
        assert "packages/conformance/pyproject.toml" in paths
        assert "packages/conformance/CHANGELOG.md" in paths


# ---------------------------------------------------------------------------
# evaluate -- allowed cases
# ---------------------------------------------------------------------------


class TestAllowed:
    def test_conformance_bump_branch_allows_conformance_files(self):
        parsed = grf.parse_diff(
            CONFORMANCE_VERSION_DIFF
            + CONFORMANCE_DUNDER_DIFF
            + CONFORMANCE_CHANGELOG_DIFF
        )
        assert grf.evaluate(parsed, "bump-version-conformance") == []

    def test_toolkit_bump_branch_allows_toolkit_files(self):
        parsed = grf.parse_diff(TOOLKIT_VERSION_DIFF + TOOLKIT_CHANGELOG_DIFF)
        assert grf.evaluate(parsed, "bump-version-contract-toolkit") == []

    def test_conformance_bump_branch_does_not_exempt_toolkit_files(self):
        # A branch may only touch the files of the package it releases.
        parsed = grf.parse_diff(TOOLKIT_VERSION_DIFF)
        violations = grf.evaluate(parsed, "bump-version-conformance")
        assert "contract-toolkit/src/PklProject" in [p for p, _ in violations]

    def test_toolkit_bump_branch_does_not_exempt_conformance_files(self):
        # The symmetric inverse: the toolkit release branch must not exempt
        # conformance's release-owned files.
        parsed = grf.parse_diff(CONFORMANCE_VERSION_DIFF)
        violations = grf.evaluate(parsed, "bump-version-contract-toolkit")
        assert "packages/conformance/pyproject.toml" in [p for p, _ in violations]

    def test_dependency_edit_leaves_version_line_untouched(self):
        parsed = grf.parse_diff(CONFORMANCE_DEP_ONLY_DIFF)
        assert grf.evaluate(parsed, "feature/bump-deps") == []

    def test_unrelated_source_change(self):
        assert grf.evaluate(grf.parse_diff(UNRELATED_DIFF), "feature/x") == []

    def test_sdk_version_is_not_guarded(self):
        assert grf.evaluate(grf.parse_diff(SDK_VERSION_DIFF), "feature/x") == []


# ---------------------------------------------------------------------------
# run -- output + sticky comment
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_clean(self, tmp_path):
        diff = tmp_path / "pr.diff"
        diff.write_text(UNRELATED_DIFF)
        comment = tmp_path / "comment.md"
        violation, violations = grf.run(str(diff), "feature/x", str(comment))
        assert violation is False
        assert violations == []
        assert not comment.exists()

    def test_run_violation_writes_comment(self, tmp_path):
        diff = tmp_path / "pr.diff"
        diff.write_text(CONFORMANCE_VERSION_DIFF)
        comment = tmp_path / "comment.md"
        violation, _ = grf.run(str(diff), "feature/x", str(comment))
        assert violation is True
        assert comment.exists()
        body = comment.read_text()
        assert "packages/conformance/pyproject.toml" in body
        assert "release branch" in body

    def test_run_violation_suppressed_on_bump_branch(self, tmp_path):
        diff = tmp_path / "pr.diff"
        diff.write_text(CONFORMANCE_VERSION_DIFF)
        comment = tmp_path / "comment.md"
        violation, _ = grf.run(str(diff), "bump-version-conformance", str(comment))
        assert violation is False
        assert not comment.exists()
