"""The conformance wheel gate is wired before anything ships (FND-4176).

The gate itself was run red-green against real wheels when it was written: a
wheel carrying the 0.51.0 cycle fails the import smoke, and a wheel with its
SARIF schema stripped imports cleanly but fails the test slice. Those runs
need ``uv`` and the network, so they are not repeated here. This pins what a
later edit could silently undo: the gate's position in both workflows, the
slice it runs, and that a failing step fails the gate (``main`` driven over a
stubbed ``subprocess.run``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conformance_wheel_gate as gate  # noqa: E402

WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
GATE_CMD = "python .github/scripts/conformance_wheel_gate.py"


def _step_runs(workflow: str, job: str) -> list[str]:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text())
    return [step.get("run", "") for step in data["jobs"][job]["steps"]]


def _index(runs: list[str], needle: str) -> int:
    matches = [i for i, run in enumerate(runs) if needle in run]
    assert len(matches) == 1, f"{needle!r} in {len(matches)} steps"
    return matches[0]


def test_release_runs_gate_after_build_before_push() -> None:
    runs = _step_runs("conformance-release.yml", "open-release-pr")
    build = _index(runs, "uv build")
    smoke = _index(runs, GATE_CMD)
    push = _index(runs, "git push")
    assert build < smoke < push


def test_publish_runs_gate_between_build_and_publish() -> None:
    runs = _step_runs("conformance-publish.yml", "publish")
    build = _index(runs, "uv build")
    smoke = _index(runs, GATE_CMD)
    publish = _index(runs, "uv publish")
    assert build < smoke < publish


@pytest.mark.parametrize("name", gate.SLICE)
def test_slice_files_exist(name: str) -> None:
    """A renamed test file would otherwise fail the release, not the PR."""
    assert (gate.PACKAGE_DIR / "tests" / name).is_file()


def _step(argv: Sequence[str]) -> str:
    if argv[:2] == ["uv", "export"]:
        return "export"
    if argv[:2] == ["uv", "venv"]:
        return "venv"
    if argv[:3] == ["uv", "pip", "install"]:
        return "install"
    if str(gate.SMOKE) in argv:
        return "smoke"
    if "pytest" in argv:
        return "pytest"
    raise AssertionError(f"unexpected command {argv}")


class _FakeRun:
    """Stands in for subprocess.run: records each step, returns its set code."""

    def __init__(self, codes: dict[str, int]) -> None:
        self.codes = codes
        self.calls: list[tuple[str, list[str], Path]] = []

    def __call__(
        self, argv: Sequence[str], cwd: Path, check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        step = _step(list(argv))
        self.calls.append((step, list(argv), Path(cwd)))
        code = self.codes.get(step, 0)
        if check and code:
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code)

    @property
    def steps(self) -> list[str]:
        return [step for step, _, _ in self.calls]


@pytest.fixture
def wheel_dir(tmp_path: Path) -> Path:
    (tmp_path / "atlan_application_sdk_conformance-1.0.0-py3-none-any.whl").touch()
    return tmp_path


def _drive(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path, **codes: int
) -> tuple[_FakeRun, int | BaseException]:
    fake = _FakeRun(codes)
    monkeypatch.setattr(gate.subprocess, "run", fake)
    monkeypatch.setattr(gate.shutil, "copytree", lambda src, dst: None)
    try:
        return fake, gate.main(["--wheel-dir", str(wheel_dir)])
    except subprocess.CalledProcessError as exc:
        return fake, exc


def test_main_passes_when_every_step_passes(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path
) -> None:
    fake, result = _drive(monkeypatch, wheel_dir)
    assert result == 0
    assert fake.steps == ["export", "venv", "install", "smoke", "pytest"]
    _, smoke, _ = fake.calls[3]
    assert smoke[smoke.index("--outside") + 1] == str(gate.REPO_ROOT)
    _, pytest_argv, pytest_cwd = fake.calls[4]
    assert [f"tests/{name}" for name in gate.SLICE] == pytest_argv[-len(gate.SLICE) :]
    assert not pytest_cwd.is_relative_to(gate.REPO_ROOT)


def test_main_fails_and_skips_slice_when_smoke_fails(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path
) -> None:
    fake, result = _drive(monkeypatch, wheel_dir, smoke=1)
    assert result == 1
    assert "pytest" not in fake.steps


def test_main_fails_when_slice_fails(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path
) -> None:
    fake, result = _drive(monkeypatch, wheel_dir, pytest=1)
    assert result == 1
    assert fake.steps[-1] == "pytest"


@pytest.mark.parametrize("step", ["export", "venv", "install"])
def test_main_fails_when_setup_fails(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path, step: str
) -> None:
    fake, result = _drive(monkeypatch, wheel_dir, **{step: 2})
    assert isinstance(result, subprocess.CalledProcessError)
    assert fake.steps[-1] == step


def test_find_wheel_wants_exactly_one(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        gate.find_wheel(tmp_path)
    wheel = tmp_path / "atlan_application_sdk_conformance-1.0.0-py3-none-any.whl"
    wheel.touch()
    (tmp_path / "atlan_application_sdk_conformance-1.0.0.tar.gz").touch()
    assert gate.find_wheel(tmp_path) == wheel
    (tmp_path / "atlan_application_sdk_conformance-0.9.0-py3-none-any.whl").touch()
    with pytest.raises(SystemExit):
        gate.find_wheel(tmp_path)
