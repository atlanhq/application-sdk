"""Fetching a PR's base commit must not change how deep the clone is (FND-4987).

``git fetch --depth=1 origin <sha>`` in a FULL clone records ``<sha>`` in
``.git/shallow`` as a history cut-off, even when the commit is already there.
Every worktree of a repo shares that file, so one ratchet run with
``--base-sha`` in an agent worktree turned the developer's main checkout
shallow: ``main`` then read "ahead N, behind 1" against ``origin/main`` for
commits that were already upstream.

Both base-fetching scripts are driven against real repositories over a
``file://`` remote, because the defect is git's on-disk side effect, which a
stubbed runner cannot show. The runners only record which commands ran and
pass every one through to real git.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_pyright_baseline as cpb  # noqa: E402
import check_uv_lock  # noqa: E402

BASELINE_REL = ".github/scripts/pyright_baseline.json"

#: Runs the script's base handling in ``clone`` for ``base``; returns the
#: ``git ...`` argv lists it ran.
Driver = Callable[[Path, str, pytest.MonkeyPatch], list[list[str]]]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _origin(tmp_path: Path) -> tuple[Path, str]:
    """A two-commit origin; returns it and its ``main`` tip."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    (origin / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _commit(origin, "pyproject.toml")
    return origin, _commit(origin, "a.py")


def _clone(origin: Path, dest: Path, *flags: str) -> Path:
    _git(origin.parent, "clone", "-q", *flags, origin.as_uri(), str(dest))
    return dest


def _later_base(origin: Path) -> str:
    """A commit the clone has not seen: the base moved after it was cloned."""
    _git(origin, "checkout", "-q", "-b", "next")
    return _commit(origin, "b.py")


def _is_shallow(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--is-shallow-repository") == "true"


def _fetches(ran: list[list[str]]) -> list[list[str]]:
    return [cmd for cmd in ran if cmd[1:2] == ["fetch"]]


def _drive_pyright(
    clone: Path, base: str, monkeypatch: pytest.MonkeyPatch
) -> list[list[str]]:
    ran: list[list[str]] = []

    def git(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        ran.append(["git", *cmd])
        return subprocess.run(
            ["git", *cmd], cwd=clone, capture_output=True, text=True, check=False
        )

    # The base carries no baseline file: the commit is readable, the file absent.
    assert cpb.read_base_baseline(base, BASELINE_REL, git) is None
    return ran


def _drive_uv_lock(
    clone: Path, base: str, monkeypatch: pytest.MonkeyPatch
) -> list[list[str]]:
    ran: list[list[str]] = []
    monkeypatch.chdir(clone)

    def quiet(cmd: Sequence[str]) -> int:
        ran.append(list(cmd))
        return subprocess.run(list(cmd), check=False, capture_output=True).returncode

    # Base readable: the answer comes from the diff, not the "unknown" fallback.
    check_uv_lock.lock_changed(base, quiet)
    assert quiet(["git", "cat-file", "-e", f"{base}^{{commit}}"]) == 0
    return ran


DRIVERS = pytest.mark.parametrize(
    "drive",
    [
        pytest.param(_drive_pyright, id="check_pyright_baseline"),
        pytest.param(_drive_uv_lock, id="check_uv_lock"),
    ],
)


@DRIVERS
def test_full_clone_with_the_base_present_does_not_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drive: Driver
) -> None:
    origin, base = _origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    assert not _is_shallow(clone)

    ran = drive(clone, base, monkeypatch)

    assert _fetches(ran) == []
    assert not _is_shallow(clone)
    assert not (clone / ".git" / "shallow").exists()


@DRIVERS
def test_full_clone_missing_the_base_fetches_without_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drive: Driver
) -> None:
    origin, _ = _origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    base = _later_base(origin)

    ran = drive(clone, base, monkeypatch)

    fetches = _fetches(ran)
    assert len(fetches) == 1
    assert not any(arg.startswith("--depth") for arg in fetches[0])
    assert fetches[0][-1] == base
    assert not _is_shallow(clone)


@DRIVERS
def test_shallow_clone_still_fetches_one_commit_deep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drive: Driver
) -> None:
    """CI checks out shallow: its fetch must stay ``--depth=1``, not pull history."""
    origin, _ = _origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone", "--depth=1")
    assert _is_shallow(clone)
    base = _later_base(origin)

    ran = drive(clone, base, monkeypatch)

    fetches = _fetches(ran)
    assert len(fetches) == 1
    assert "--depth=1" in fetches[0]
    assert fetches[0][-1] == base
    assert _is_shallow(clone)
