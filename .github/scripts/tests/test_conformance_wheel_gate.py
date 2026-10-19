"""The conformance wheel gate is wired before anything ships (FND-4176).

The gate itself was run red-green against real wheels when it was written: a
wheel carrying the 0.51.0 cycle fails the import smoke, and a wheel with its
SARIF schema stripped imports cleanly but fails the test slice. Those runs
need ``uv`` and the network, so they are not repeated here; this pins what a
later edit could silently undo — the gate's position in both workflows and the
slice it runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

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
