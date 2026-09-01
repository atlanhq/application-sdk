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


# ---------------------------------------------------------------------------
# Untagged repositories — the no-tag branch of _resolve_rev_range
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_root_untagged_repo(tmp_path: Path) -> Path:
    """Return a temp git repo with no release tag and *two* root commits.

    Reproduces a repository whose history was grafted from two independent
    starting points and that has never been tagged. That combination made the
    previous `git rev-list --max-parents=0 HEAD` fallback emit one SHA per root,
    producing a multi-line value that split the subsequent `git log` command in
    two and aborted the bump with exit 127.
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

    # First root
    (tmp_path / "a.txt").write_text("a\n")
    git("add", ".")
    git("commit", "-m", "feat: first history")
    main_branch = (
        subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=tmp_path
        )
        .decode()
        .strip()
    )

    # Second, entirely unrelated root
    git("checkout", "--orphan", "second-root")
    git("rm", "-rf", ".")
    (tmp_path / "b.txt").write_text("b\n")
    git("add", ".")
    git("commit", "-m", "fix: second history")

    # Graft them together — HEAD now reaches two root commits
    git("checkout", main_branch)
    git("merge", "--allow-unrelated-histories", "--no-edit", "second-root")

    return tmp_path


class TestUntaggedRepo:
    def test_fixture_really_has_two_roots(self, multi_root_untagged_repo: Path) -> None:
        """Guard the fixture: without two roots the regression below is vacuous."""
        roots = subprocess.check_output(
            ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=multi_root_untagged_repo
        )
        assert len(roots.decode().strip().split("\n")) == 2

    def test_rev_range_is_head_when_untagged(
        self, multi_root_untagged_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No release tag must resolve to a bare HEAD walk, never a `a..HEAD` range."""
        monkeypatch.chdir(multi_root_untagged_repo)
        assert release._resolve_rev_range() == ["HEAD"]

    def test_multi_root_untagged_repo_does_not_crash(
        self, multi_root_untagged_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: this raised CalledProcessError (exit 127) before the fix."""
        monkeypatch.chdir(multi_root_untagged_repo)
        commits = release.get_commits_since_last_tag()
        # Both roots must be represented — a `head -1` style fix would drop one.
        assert "feat: first history" in commits
        assert "fix: second history" in commits

    def test_tagged_repo_still_uses_a_range(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tagged path is unchanged: walk from the last release tag."""
        monkeypatch.chdir(git_repo)
        assert release._resolve_rev_range() == ["v1.0.0..HEAD"]

    def test_last_release_tag_is_none_when_untagged(
        self, multi_root_untagged_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(multi_root_untagged_repo)
        assert release.last_release_tag() is None

    def test_last_release_tag_returns_the_tag_when_tagged(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(git_repo)
        assert release.last_release_tag() == "v1.0.0"


# ---------------------------------------------------------------------------
# apply_first_release_floor — apps' first release is a 1.0.0 event
# ---------------------------------------------------------------------------


class TestApplyFirstReleaseFloor:
    def test_first_release_below_floor_is_raised(self) -> None:
        """An app scaffolded at 0.1.0 releases as 1.0.0, not 0.2.0."""
        assert (
            release.apply_first_release_floor(
                "0.2.0", floor="1.0.0", has_release_tag=False
            )
            == "1.0.0"
        )

    def test_first_release_already_at_floor_bumps_normally(self) -> None:
        """A floor, not an assignment.

        13 of the 36 currently-untagged apps sit at exactly 1.0.0. Forcing the
        floor there would emit a bump PR that does not change the version.
        """
        assert (
            release.apply_first_release_floor(
                "1.0.1", floor="1.0.0", has_release_tag=False
            )
            == "1.0.1"
        )

    def test_first_release_past_floor_bumps_normally(self) -> None:
        assert (
            release.apply_first_release_floor(
                "2.3.1", floor="1.0.0", has_release_tag=False
            )
            == "2.3.1"
        )

    def test_already_released_repo_is_untouched(self) -> None:
        """Only the *first* release gets the floor — later ones bump normally."""
        assert (
            release.apply_first_release_floor(
                "0.2.0", floor="1.0.0", has_release_tag=True
            )
            == "0.2.0"
        )

    @pytest.mark.parametrize("floor", ["", None])
    def test_empty_floor_disables_the_behaviour(self, floor: str | None) -> None:
        """The SDK's own release passes no floor and must be unaffected."""
        assert (
            release.apply_first_release_floor(
                "0.2.0", floor=floor, has_release_tag=False
            )
            == "0.2.0"
        )


class TestMainAlreadyReleasedGuard:
    """release.main() must not bump to a version the target branch already has.

    Reproduces application-sdk#3570 in the lane shared by the SDK's own release
    and all app repos: a run whose checkout is a frozen merge ref predating the
    release merge recomputes the version that just shipped.
    """

    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        guard_result: tuple,
        branch: str = "main",
        new_version: str = "1.3.0",
    ) -> tuple[list, dict, dict]:
        monkeypatch.setattr(sys, "argv", ["release.py", branch, "1.2.0"])
        monkeypatch.setattr(release, "get_commits_since_last_tag", lambda: ["feat: x"])
        monkeypatch.setattr(release, "last_release_tag", lambda: "v1.2.0")
        monkeypatch.setattr(release, "calculate_version_bump", lambda **_k: new_version)

        written: list = []
        monkeypatch.setattr(
            release,
            "update_pyproject_version",
            lambda **kw: written.append(kw["new_version"]),
        )

        outputs: dict = {}
        monkeypatch.setattr(release, "_set_output", lambda k, v: outputs.update({k: v}))

        seen: dict = {}

        def fake_guard(path, version, **kwargs):
            seen["path"] = path
            seen["version"] = version
            seen["branch"] = kwargs.get("branch")
            return guard_result

        monkeypatch.setattr(release.release_guard, "already_released", fake_guard)
        return written, outputs, seen

    def test_skips_without_writing_pyproject(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        written, outputs, _ = self._wire(monkeypatch, guard_result=(True, "1.3.0"))

        release.main()

        # The callers gate their changelog and commit steps on this output.
        assert outputs["skip"] == "true"
        assert "new" not in outputs
        assert written == []

    def test_normal_release_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        written, outputs, _ = self._wire(monkeypatch, guard_result=(False, "1.2.0"))

        release.main()

        assert outputs["skip"] == "false"
        assert outputs["new"] == "1.3.0"
        assert written == ["1.3.0"]

    def test_guard_is_asked_about_the_target_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """App repos may release off a branch other than main."""
        _w, _o, seen = self._wire(
            monkeypatch, guard_result=(False, None), branch="release-2.x"
        )

        release.main()

        assert seen["branch"] == "release-2.x"
        assert seen["version"] == "1.3.0"
        assert seen["path"] == "pyproject.toml"
