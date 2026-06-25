"""Tests for .github/scripts/release.py.

Three coverage areas:
  1. _SUBPKG_RE — parametrised match/no-match table
  2. parse_conventional_commits — key cases including the previously-overmatched
     'chore: featured update' subject
  3. get_commits_since_last_tag — integration test using a real temp git repo
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import release

# ---------------------------------------------------------------------------
# _SUBPKG_RE — sub-package commit scope filter
# ---------------------------------------------------------------------------


class TestSubpkgRe:
    """_SUBPKG_RE must match every conventionally-scoped sub-package subject and
    pass every legitimate SDK subject — including ones that embed 'feat' or a
    sub-package name in a position other than the scope field."""

    @pytest.mark.parametrize(
        "subject",
        [
            # conformance scope — regular and breaking
            "feat(conformance): add E020 rule",
            "fix(conformance): correct regex anchor",
            "chore(conformance): update deps",
            "refactor(conformance): reorganise checks",
            "feat(conformance)!: breaking API change",
            "fix(conformance)!: drop Python 3.10 support",
            # contract-toolkit scope — regular and breaking
            "feat(contract-toolkit): add schema",
            "fix(contract-toolkit): repair validator",
            "refactor(contract-toolkit)!: remove legacy hook",
            "chore(contract-toolkit): bump uv",
        ],
    )
    def test_matches_subpkg_subjects(self, subject: str) -> None:
        assert release._SUBPKG_RE.match(subject), f"expected a match: {subject!r}"

    @pytest.mark.parametrize(
        "subject",
        [
            # plain SDK commits
            "feat: add SDK feature",
            "fix: repair connection handling",
            "chore: bump deps",
            "docs: update readme",
            # scoped to other SDK sub-systems — not sub-packages
            "feat(api): add new endpoint",
            "fix(temporal): correct retry policy",
            "docs(sdk): update api guide",
            # 'feat' appears in the description, not the type — must not trigger
            "refactor: feat-flag X",
            "chore: remove featured toggle",
            # scope string that contains a sub-package name but is not an exact match
            "docs(conformance-docs): update",  # hyphen-extended scope
            "feat(conformance-extras): test",  # not exactly 'conformance'
            # empty subject
            "",
        ],
    )
    def test_no_match_for_sdk_subjects(self, subject: str) -> None:
        assert not release._SUBPKG_RE.match(subject), f"unexpected match: {subject!r}"


# ---------------------------------------------------------------------------
# parse_conventional_commits
# ---------------------------------------------------------------------------


class TestParseConventionalCommits:
    def test_feat_gives_feature_flag(self) -> None:
        assert release.parse_conventional_commits(["feat: add new endpoint"]) == (
            False,
            True,
            False,
        )

    def test_feat_scoped_gives_feature_flag(self) -> None:
        assert release.parse_conventional_commits(["feat(api): new method"]) == (
            False,
            True,
            False,
        )

    def test_fix_gives_fix_flag(self) -> None:
        assert release.parse_conventional_commits(["fix: repair regex"]) == (
            False,
            False,
            True,
        )

    def test_breaking_exclamation_gives_breaking(self) -> None:
        assert release.parse_conventional_commits(["feat!: drop Python 3.10"]) == (
            True,
            False,
            False,
        )

    def test_breaking_change_marker_gives_breaking(self) -> None:
        assert release.parse_conventional_commits(
            ["BREAKING CHANGE: old API removed"]
        ) == (
            True,
            False,
            False,
        )

    def test_empty_list_gives_all_false(self) -> None:
        assert release.parse_conventional_commits([]) == (False, False, False)

    def test_chore_gives_all_false(self) -> None:
        assert release.parse_conventional_commits(["chore: bump deps"]) == (
            False,
            False,
            False,
        )

    def test_chore_featured_update_does_not_trigger_feature(self) -> None:
        """The word 'feat' embedded mid-string must not trigger is_feature.

        Before the ^feat[(!:] anchor was added, re.search('feat') would match
        'featured' in 'chore: featured update' and incorrectly count the commit
        as a new feature.  The anchored pattern must return False for is_feature.
        """
        _, is_feature, _ = release.parse_conventional_commits(
            ["chore: featured update"]
        )
        assert not is_feature

    def test_refactor_feat_flag_does_not_trigger_feature(self) -> None:
        """'refactor: feat-flag X' must not trigger is_feature."""
        _, is_feature, _ = release.parse_conventional_commits(["refactor: feat-flag X"])
        assert not is_feature

    def test_breaking_takes_precedence_over_feat(self) -> None:
        commits = ["feat!: overhaul", "feat: new thing"]
        is_breaking, _, _ = release.parse_conventional_commits(commits)
        assert is_breaking

    def test_mixed_feat_and_fix(self) -> None:
        commits = ["feat: new feature", "fix: bug fix", "chore: housekeeping"]
        is_breaking, is_feature, is_fix = release.parse_conventional_commits(commits)
        assert not is_breaking
        assert is_feature
        assert is_fix


# ---------------------------------------------------------------------------
# get_commits_since_last_tag — integration via a real temp git repo
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Return a temp git repo with commits both before and after a v1.0.0 tag.

    Commits after the tag:
      - feat: add SDK feature              (SDK-level — must appear)
      - feat(conformance): add E020 check  (sub-package scope — must be filtered)
      - fix(contract-toolkit): repair      (sub-package scope — must be filtered)
      - fix: correct connection handling   (SDK-level — must appear)
    """

    def git(*args: str) -> None:
        subprocess.check_call(
            ["git", *args],
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")

    # Baseline commit and lightweight release tag
    (tmp_path / "README.md").write_text("init\n")
    git("add", ".")
    git("commit", "-m", "chore: initial commit")
    git("tag", "v1.0.0")

    # SDK-level feature — should appear in results
    (tmp_path / "sdk.py").write_text("sdk\n")
    git("add", ".")
    git("commit", "-m", "feat: add SDK feature")

    # Conformance-scoped commit — subject filtered by _SUBPKG_RE
    (tmp_path / "sdk.py").write_text("sdk-2\n")
    git("add", ".")
    git("commit", "-m", "feat(conformance): add E020 check")

    # Contract-toolkit-scoped commit — subject filtered by _SUBPKG_RE
    (tmp_path / "sdk.py").write_text("sdk-3\n")
    git("add", ".")
    git("commit", "-m", "fix(contract-toolkit): repair schema")

    # SDK-level fix — should appear in results
    (tmp_path / "sdk.py").write_text("sdk-4\n")
    git("add", ".")
    git("commit", "-m", "fix: correct connection handling")

    return tmp_path


class TestGetCommitsSinceLastTag:
    def test_subpkg_scoped_subjects_are_removed(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Subjects scoped to sub-packages must not appear in the returned list."""
        monkeypatch.chdir(git_repo)
        commits = release.get_commits_since_last_tag()
        assert "feat: add SDK feature" in commits
        assert "fix: correct connection handling" in commits
        assert not any("conformance" in c for c in commits)
        assert not any("contract-toolkit" in c for c in commits)

    def test_empty_lines_are_removed(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The returned list must contain no blank or whitespace-only elements."""
        monkeypatch.chdir(git_repo)
        commits = release.get_commits_since_last_tag()
        assert all(c.strip() for c in commits)

    def test_returns_list_type(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(git_repo)
        assert isinstance(release.get_commits_since_last_tag(), list)
