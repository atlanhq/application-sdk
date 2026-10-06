"""Tests for .github/scripts/release_bump_debounce.py (FND-3322).

Driven against real git repositories — a bare ``origin`` plus a clone — because
the decision is made from what ``git fetch``/``git show``/``git merge-tree``
report, and a stubbed git would only prove the stub.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import release_bump_debounce as mod

BRANCH = "bump-version-main"

PYPROJECT = '[project]\nname = "app"\nversion = "{v}"\n\n[tool.x]\nversion = "9.9.9"\n'
LOCK = 'version = 1\n\n[[package]]\nname = "app"\nversion = "{v}"\n\n[[package]]\nname = "dep"\nversion = "{dep}"\n'


def _changelog(version: str, date: str, fixes: list[str]) -> str:
    body = "".join(f"- {f}\n" for f in fixes)
    section = f"## v{version} ({date})\n\nFull Changelog: x\n\n"
    if fixes:
        section += f"### Bug Fixes\n\n{body}\n"
    return f"# Changelog\n\n{section}\n## v1.0.0 (January 01, 2026)\n\nold\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")


def _commit(repo: Path, msg: str, files: dict[str, str]) -> None:
    _write(repo, files)
    _git(repo, "add", *files)
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone of a bare origin whose main is at 1.0.0 with one dependency."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "ci@example.com")
    _git(work, "config", "user.name", "ci")
    _git(work, "checkout", "-q", "-b", "main")
    _commit(
        work,
        "init",
        {
            "pyproject.toml": PYPROJECT.format(v="1.0.0"),
            "uv.lock": LOCK.format(v="1.0.0", dep="1.0"),
            "CHANGELOG.md": "# Changelog\n\n## v1.0.0 (January 01, 2026)\n\nold\n",
            "app.py": "x = 1\n",
        },
    )
    _git(work, "push", "-q", "origin", "main")
    monkeypatch.chdir(work)
    return work


def _publish_bump(repo: Path, version: str, date: str, fixes: list[str]) -> None:
    """Build a bump commit on the current main and force-push it to BRANCH."""
    _git(repo, "checkout", "-q", "-B", BRANCH, "main")
    _commit(
        repo,
        f"chore: bump version to {version}",
        {
            "pyproject.toml": PYPROJECT.format(v=version),
            "uv.lock": LOCK.format(
                v=version, dep=_git(repo, "show", "main:uv.lock").split('"')[-2]
            ),
            "CHANGELOG.md": _changelog(version, date, fixes),
        },
    )
    _git(repo, "push", "-q", "-f", "origin", BRANCH)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-q", "-D", BRANCH)


def _merge_to_main(repo: Path, msg: str, files: dict[str, str]) -> None:
    _commit(repo, msg, files)
    _git(repo, "push", "-q", "origin", "main")


def _build_locally(version: str, date: str, fixes: list[str]) -> None:
    """What the workflow's bump + changelog steps leave in the worktree."""
    _write(
        Path.cwd(),
        {
            "pyproject.toml": PYPROJECT.format(v=version),
            "CHANGELOG.md": _changelog(version, date, fixes),
        },
    )


def _decide(version: str, version_files: list[str] | None = None) -> tuple[bool, str]:
    return mod.decide(
        branch=BRANCH,
        version_files=version_files or ["pyproject.toml"],
        changelog="CHANGELOG.md",
        new_version=version,
        workdir=Path.cwd(),
    )


def test_pushes_when_the_bump_branch_does_not_exist(repo: Path) -> None:
    _build_locally("1.0.1", "October 06, 2026", ["a fix"])
    push, reason = _decide("1.0.1")
    assert push is True
    assert "could not be fetched" in reason


def test_skips_a_renovate_chore_merge_that_changes_neither_version_nor_notes(
    repo: Path,
) -> None:
    _merge_to_main(repo, "fix: a fix", {"app.py": "x = 2\n"})
    _publish_bump(repo, "1.0.1", "October 05, 2026", ["a fix"])
    # A chore(deps) merge moves uv.lock on main; the next day's run rebuilds the
    # same version and the same notes (chores are not rendered).
    _merge_to_main(
        repo, "chore(deps): bump dep", {"uv.lock": LOCK.format(v="1.0.0", dep="2.0")}
    )
    _build_locally("1.0.1", "October 06, 2026", ["a fix"])

    push, reason = _decide("1.0.1")

    assert push is False, reason
    # And the stale branch really does combine with the new main: the bump's
    # version edit and Renovate's dependency edit merge without conflict.
    merged = _git(repo, "merge-tree", "--write-tree", "HEAD", f"origin/{BRANCH}")
    lock = _git(repo, "show", f"{merged.splitlines()[0]}:uv.lock")
    assert 'version = "1.0.1"' in lock and 'version = "2.0"' in lock


def test_pushes_when_a_feat_merge_turns_the_patch_into_a_minor(repo: Path) -> None:
    _publish_bump(repo, "1.0.1", "October 06, 2026", [])
    _merge_to_main(repo, "feat: thing", {"app.py": "x = 3\n"})
    _build_locally("1.1.0", "October 06, 2026", [])

    push, reason = _decide("1.1.0")

    assert push is True
    assert "version changed" in reason and "1.0.1" in reason


def test_pushes_when_a_fix_merge_adds_a_release_note(repo: Path) -> None:
    _publish_bump(repo, "1.0.1", "October 06, 2026", ["first fix"])
    _merge_to_main(repo, "fix: second", {"app.py": "x = 4\n"})
    _build_locally("1.0.1", "October 06, 2026", ["first fix", "second"])

    push, reason = _decide("1.0.1")

    assert push is True
    assert "release notes" in reason


def test_pushes_when_the_open_branch_no_longer_merges_cleanly(repo: Path) -> None:
    _publish_bump(repo, "1.0.1", "October 06, 2026", [])
    # A human edit to the same CHANGELOG lines the bump prepends conflicts.
    _merge_to_main(
        repo,
        "docs: hand-edit changelog",
        {
            "CHANGELOG.md": "# Changelog\n\nhand edit\n\n## v1.0.0 (January 01, 2026)\n\nold\n"
        },
    )
    _build_locally("1.0.1", "October 06, 2026", [])

    push, reason = _decide("1.0.1")

    assert push is True
    assert "no longer merges cleanly" in reason


def test_every_version_file_must_match(repo: Path) -> None:
    """The SDK's own flow also carries the version in application_sdk/version.py."""
    _publish_bump(repo, "1.0.1", "October 06, 2026", [])
    _build_locally("1.0.1", "October 06, 2026", [])

    push, reason = _decide("1.0.1", ["pyproject.toml", "version.py"])

    assert push is True
    assert "version.py is missing" in reason


def test_main_fails_open_and_writes_outputs(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "gho"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    def boom(cmd):
        raise OSError("no git")

    monkeypatch.setattr(mod, "run", boom)

    assert (
        mod.main(
            [
                "--branch",
                BRANCH,
                "--version-file",
                "pyproject.toml",
                "--new-version",
                "1.0.1",
            ]
        )
        == 0
    )
    lines = out.read_text().splitlines()
    assert "push=true" in lines
    assert any(line.startswith("reason=debounce check failed") for line in lines)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (PYPROJECT.format(v="2.3.4"), "2.3.4"),
        ('"""doc"""\n__version__ = "0.5.0"\n__dapr_version = "1"\n', "0.5.0"),
        ("nothing here\n", None),
    ],
)
def test_read_version_takes_the_first_top_level_assignment(
    text: str, expected: str | None
) -> None:
    assert mod.read_version(text) == expected


def test_changelog_section_ignores_the_heading_date() -> None:
    a = mod.changelog_section(_changelog("1.0.1", "October 05, 2026", ["x"]), "1.0.1")
    b = mod.changelog_section(_changelog("1.0.1", "October 06, 2026", ["x"]), "1.0.1")
    assert a is not None and a == b
    assert "v1.0.0" not in a  # stops at the next section
    assert mod.changelog_section(_changelog("1.0.1", "d", []), "1.0.2") is None


def test_a_stalled_git_call_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fetch that hangs must read as "cannot tell", so the bump is pushed."""

    def stall(*_a, **_k):
        raise subprocess.TimeoutExpired(["git", "fetch"], mod.GIT_TIMEOUT_S)

    monkeypatch.setattr(mod.subprocess, "run", stall)
    assert mod.run(["git", "fetch", "origin", "x"]).returncode != 0
