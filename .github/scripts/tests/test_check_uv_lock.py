"""The Pre-commit lock check rejects a refused Renovate lock (FND-3328).

Red-green against the real `uv` and the real refusal writer
(`renovate_uv_lock_bounded.withhold`), so a change to either the tripwire's
shape or uv's validation shows up here rather than as a refused lock PR that
auto-merges.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_uv_lock  # noqa: E402
import renovate_uv_lock_bounded as bounded  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
UV = shutil.which("uv")
needs_uv = pytest.mark.skipif(UV is None, reason="uv not on PATH")

PYPROJECT = """\
[project]
name = "lock-check-demo"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []
"""


def _project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    subprocess.run(["uv", "lock", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _uv_in(root: Path):
    def run(cmd: Sequence[str]) -> int:
        return subprocess.run(list(cmd), cwd=root, check=False).returncode

    return run


def _changed(cmd: Sequence[str]) -> int:
    # fetch ok, diff says "differs"
    return 1 if cmd[:2] == ["git", "diff"] else 0


@needs_uv
def test_valid_lock_passes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    assert check_uv_lock.main(root, {"BASE_SHA": ""}, _uv_in(root), _changed) == 0


@needs_uv
@pytest.mark.parametrize(
    "reason", [bounded.REFUSAL_WINDOW_EMPTY, bounded.REFUSAL_ROLLBACK]
)
def test_refused_lock_fails(tmp_path: Path, reason: str) -> None:
    root = _project(tmp_path)
    lock = root / "uv.lock"
    assert bounded.withhold(lock, lock.read_text(), "P7D", reason=reason)
    assert "[options]" in lock.read_text()
    assert check_uv_lock.main(root, {"BASE_SHA": "abc"}, _uv_in(root), _changed) == 1


def test_no_lock_passes_without_running_uv(tmp_path: Path) -> None:
    def run(cmd: Sequence[str]) -> int:
        raise AssertionError("uv must not run")

    assert check_uv_lock.main(tmp_path, {}, run, _changed) == 0


def test_unchanged_lock_is_not_rechecked(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("version = 1\n")

    def run(cmd: Sequence[str]) -> int:
        raise AssertionError("uv must not run on an unchanged lock")

    assert check_uv_lock.main(tmp_path, {"BASE_SHA": "abc"}, run, lambda cmd: 0) == 0


@pytest.mark.parametrize(
    ("base", "fetch", "diff", "changed"),
    [
        ("", 0, 0, True),  # no base: check
        ("abc", 128, 0, True),  # fetch failed: check
        ("abc", 0, 128, True),  # diff errored: check
        ("abc", 0, 1, True),
        ("abc", 0, 0, False),
    ],
)
def test_lock_changed_fails_towards_checking(
    base: str, fetch: int, diff: int, changed: bool
) -> None:
    def quiet(cmd: Sequence[str]) -> int:
        return fetch if cmd[1] == "fetch" else diff

    assert check_uv_lock.lock_changed(base, quiet) is changed


def test_pre_commit_job_runs_the_lock_check_on_every_pr() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/checks-reusable.yaml").read_text()
    )
    job = workflow["jobs"]["pre-commit"]
    assert job["name"] == "Pre-commit"
    (step,) = [s for s in job["steps"] if "check_uv_lock.py" in str(s.get("run", ""))]
    assert "if" not in step
    assert step["env"]["BASE_SHA"] == (
        "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha }}"
    )
