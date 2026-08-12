"""Tests for .github/scripts/update_changelog.py.

Coverage:
  1. get_commits_since_last_tag — tagged repos keep the compare API; untagged
     repos enumerate full history via the paginated commits API (never
     `git rev-list --max-parents=0 HEAD`, which emits one SHA per root and
     breaks the compare call on multi-root repos).
  2. Untagged failure modes fail loudly — a first release must never ship an
     empty changelog with a green workflow.
  3. _format_commits — author fallbacks and API-shape tolerance.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import update_changelog

# ---------------------------------------------------------------------------
# Real-git fixtures (same shapes as test_release.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def passthrough_run(monkeypatch: pytest.MonkeyPatch):
    """Intercept only `gh api` calls; pass every other subprocess (git
    plumbing, the tag probe) through to the real subprocess.run."""
    real_run = update_changelog.subprocess.run

    def install(handler) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(cmd, *a, **k):
            if cmd[:2] == ["gh", "api"]:
                calls.append(cmd)
                return handler(cmd)
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(update_changelog.subprocess, "run", fake_run)
        return calls

    return install


def _init_repo(path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.check_call(
            ["git", *args],
            cwd=path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")


@pytest.fixture
def multi_root_untagged_repo(tmp_path: Path) -> Path:
    """A repo with no tags and two root commits — the shape that made the old
    `rev-list --max-parents=0 HEAD` fallback emit a multi-line value and break
    the compare API call."""
    _init_repo(tmp_path)

    def git(*args: str) -> None:
        subprocess.check_call(
            ["git", *args],
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

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

    git("checkout", "--orphan", "second-root")
    git("rm", "-rf", ".")
    (tmp_path / "b.txt").write_text("b\n")
    git("add", ".")
    git("commit", "-m", "fix: second history")

    git("checkout", main_branch)
    git("merge", "--allow-unrelated-histories", "--no-edit", "second-root")
    return tmp_path


@pytest.fixture
def tagged_repo(tmp_path: Path) -> Path:
    """A repo with a v1.0.0 tag and one commit after it."""
    _init_repo(tmp_path)

    def git(*args: str) -> None:
        subprocess.check_call(
            ["git", *args],
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    (tmp_path / "a.txt").write_text("a\n")
    git("add", ".")
    git("commit", "-m", "chore: initial commit")
    git("tag", "v1.0.0")
    (tmp_path / "a.txt").write_text("a2\n")
    git("add", ".")
    git("commit", "-m", "feat: post-tag work")
    return tmp_path


# ---------------------------------------------------------------------------
# _gh_api_commits — API failure and pagination-shape handling
# ---------------------------------------------------------------------------


class TestGhApiCommits:
    def test_fails_loudly_without_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No token must raise — never return [] and write an empty changelog."""
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="GH_TOKEN"):
            update_changelog._gh_api_commits("repos/o/r/commits?sha=HEAD")

    def test_fails_loudly_on_api_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A gh api failure must raise — never return [] (the old code caught
        the error and shipped the first release with an empty changelog)."""
        monkeypatch.setenv("GH_TOKEN", "test-token")
        monkeypatch.setattr(
            update_changelog.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                args=a[0], returncode=1, stdout="", stderr="HTTP 404: Not Found"
            ),
        )
        with pytest.raises(RuntimeError, match="gh api"):
            update_changelog._gh_api_commits("repos/o/r/commits?sha=HEAD")

    def test_parses_paginated_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--paginate concatenates one JSON array per page — all pages merge."""
        monkeypatch.setenv("GH_TOKEN", "test-token")
        page1 = json.dumps([{"sha": "a" * 40, "commit": {"message": "one"}}])
        page2 = json.dumps([{"sha": "b" * 40, "commit": {"message": "two"}}])
        monkeypatch.setattr(
            update_changelog.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                args=a[0], returncode=0, stdout=f"{page1}\n{page2}\n", stderr=""
            ),
        )
        commits = update_changelog._gh_api_commits("repos/o/r/commits?sha=HEAD")
        assert [c["sha"] for c in commits] == ["a" * 40, "b" * 40]


# ---------------------------------------------------------------------------
# get_commits_since_last_tag — tagged vs untagged path selection
# ---------------------------------------------------------------------------


class TestGetCommitsSinceLastTag:
    def test_fixture_really_has_two_roots(self, multi_root_untagged_repo: Path) -> None:
        """Guard the fixture: without two roots the untagged tests are vacuous."""
        roots = subprocess.check_output(
            ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=multi_root_untagged_repo
        )
        assert len(roots.decode().strip().split("\n")) == 2

    def test_untagged_enumerates_full_history_via_commits_api(
        self,
        multi_root_untagged_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        passthrough_run,
    ) -> None:
        """No tag must page repos/{o}/{r}/commits?sha=HEAD — never the compare
        API with a range derived from root commits."""
        monkeypatch.chdir(multi_root_untagged_repo)
        monkeypatch.setenv("GH_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_REPOSITORY", "octo/app")

        page = json.dumps(
            [
                {
                    "sha": "c" * 40,
                    "author": {"login": "atlan-ci"},
                    "commit": {"message": "feat: first history\n\nbody"},
                },
                {
                    "sha": "d" * 40,
                    "author": {"login": "vaibhavatlan"},
                    "commit": {"message": "fix: second history"},
                },
            ]
        )
        calls = passthrough_run(
            lambda cmd: subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=f"{page}\n", stderr=""
            )
        )

        commits = update_changelog.get_commits_since_last_tag("0.1.0")

        gh_calls = [c for c in calls if c[:2] == ["gh", "api"]]
        assert len(gh_calls) == 1
        assert "repos/octo/app/commits?sha=HEAD" in gh_calls[0]
        assert not any("compare" in arg for call in gh_calls for arg in call)
        # Oldest-first: the API returns newest first, changelog walks from the start.
        assert commits == [
            f"{'d' * 7}|vaibhavatlan|fix: second history",
            f"{'c' * 7}|atlan-ci|feat: first history",
        ]

    def test_tagged_repo_uses_compare_api(
        self,
        tagged_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        passthrough_run,
    ) -> None:
        """The tagged path is unchanged: compare v<current>...HEAD."""
        monkeypatch.chdir(tagged_repo)
        monkeypatch.setenv("GH_TOKEN", "test-token")
        monkeypatch.setenv("GITHUB_REPOSITORY", "octo/app")

        calls = passthrough_run(
            lambda cmd: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="e5f6a7b|vaibhavatlan|feat: post-tag work\n",
                stderr="",
            )
        )

        commits = update_changelog.get_commits_since_last_tag("1.0.0")

        gh_calls = [c for c in calls if c[:2] == ["gh", "api"]]
        assert len(gh_calls) == 1
        assert "repos/octo/app/compare/v1.0.0...HEAD" in gh_calls[0]
        assert commits == ["e5f6a7b|vaibhavatlan|feat: post-tag work"]

    def test_tagged_compare_failure_raises(
        self,
        tagged_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        passthrough_run,
    ) -> None:
        """Even on the tagged path an API failure must not become an empty
        changelog with a green workflow."""
        monkeypatch.chdir(tagged_repo)
        monkeypatch.setenv("GH_TOKEN", "test-token")

        passthrough_run(
            lambda cmd: subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="boom"
            )
        )

        with pytest.raises(RuntimeError, match="compare"):
            update_changelog.get_commits_since_last_tag("1.0.0")


# ---------------------------------------------------------------------------
# _format_commits
# ---------------------------------------------------------------------------


class TestFormatCommits:
    def test_prefers_github_login(self) -> None:
        commits = [
            {
                "sha": "1234567890abcdef",
                "author": {"login": "octocat"},
                "commit": {"message": "feat: thing", "author": {"name": "The Octocat"}},
            }
        ]
        assert update_changelog._format_commits(commits) == [
            "1234567|octocat|feat: thing"
        ]

    def test_falls_back_to_commit_author_name(self) -> None:
        """Commits by users outside the org have a null top-level author."""
        commits = [
            {
                "sha": "1234567890abcdef",
                "author": None,
                "commit": {"message": "fix: thing", "author": {"name": "External"}},
            }
        ]
        assert update_changelog._format_commits(commits) == [
            "1234567|External|fix: thing"
        ]

    def test_uses_first_message_line_and_reverses_order(self) -> None:
        commits = [
            {"sha": "a" * 40, "author": None, "commit": {"message": "newest\n\nbody"}},
            {"sha": "b" * 40, "author": None, "commit": {"message": "oldest"}},
        ]
        assert update_changelog._format_commits(commits) == [
            f"{'b' * 7}||oldest",
            f"{'a' * 7}||newest",
        ]
