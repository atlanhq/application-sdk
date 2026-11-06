"""A stale packages/conformance/uv.lock fails the PR, not the release (FND-4716).

Red-green against the real committed lock: it passes as is, and fails once a
copy of the real SDK pyproject raises a floor or gains a dependency without a
relock, which is the drift a root-only relock of an authlib floor produced.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_conformance_lock as check  # noqa: E402
import conformance_wheel_gate as gate  # noqa: E402

WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


def test_the_committed_lock_matches_the_sdk() -> None:
    """The tree as committed passes, so the check cannot redden every PR."""
    assert check.main() == 0


def _copy_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The real SDK and api pyprojects in tmp_path; returns the SDK's."""
    copies = {}
    for name, source in gate.LOCAL_PACKAGES.items():
        dest = tmp_path / name
        dest.mkdir()
        shutil.copy(source / "pyproject.toml", dest / "pyproject.toml")
        copies[name] = dest
    monkeypatch.setattr(gate, "LOCAL_PACKAGES", copies)
    return copies["atlan-application-sdk"] / "pyproject.toml"


def test_an_sdk_floor_the_lock_has_not_seen_fails_with_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _copy_packages(tmp_path, monkeypatch)
    text = pyproject.read_text()
    raised, count = re.subn(r'"authlib>=[^,"]+', '"authlib>=99.0', text)
    # [project] dependencies and [tool.uv] constraint-dependencies; only the
    # former is compared, raising both is the vuln-floor shape.
    assert count >= 1, "the SDK no longer declares authlib with a >= floor"
    pyproject.write_text(raised)

    assert check.main() == 1
    out = capsys.readouterr().out
    assert "authlib" in out
    assert "(cd packages/conformance && uv lock)" in out


def test_an_sdk_dependency_the_lock_has_not_seen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = _copy_packages(tmp_path, monkeypatch)
    text = pyproject.read_text()
    added = text.replace(
        "dependencies = [\n", 'dependencies = [\n    "tomli-w>=1",\n', 1
    )
    assert added != text
    pyproject.write_text(added)
    assert check.main() == 1


def test_sdk_gate_runs_it_unconditionally_in_the_changes_job() -> None:
    """The `changes` job is the one the gate fails closed on; a step `if:` would
    let a drifted lock through on the PRs it skipped."""
    data = yaml.safe_load((WORKFLOWS / "sdk-gate.yaml").read_text())
    steps = [
        s
        for s in data["jobs"]["changes"]["steps"]
        if "check_conformance_lock.py" in s.get("run", "")
    ]
    assert len(steps) == 1
    assert "if" not in steps[0]
    assert "continue-on-error" not in steps[0]
