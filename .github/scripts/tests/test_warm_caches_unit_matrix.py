"""warm-caches.yaml must warm exactly the venvs the SDK unit legs restore.

A unit leg can only restore a venv saved on its own ref or on main, and only
warm-caches.yaml saves on main for most of the matrix. GitHub Actions cannot
share a ``strategy.matrix`` across workflows, so the unit job's OS x Python
list is repeated in warm-caches.yaml; a leg added to one and not the other
silently goes back to starting cold (FND-4309).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github/workflows"
SETUP_DEPS = REPO_ROOT / ".github/actions/setup-deps/action.yaml"


def _job(workflow: str, job: str) -> dict:
    return yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"][job]


def _matrix(job: dict) -> dict[str, list[str]]:
    matrix = job["strategy"]["matrix"]
    return {axis: sorted(matrix[axis]) for axis in ("os", "python-version")}


def _setup_deps_step(name: str) -> dict:
    steps = yaml.safe_load(SETUP_DEPS.read_text())["runs"]["steps"]
    matches = [s for s in steps if s.get("name") == name]
    assert len(matches) == 1, matches
    return matches[0]


def test_warm_matrix_equals_unit_matrix():
    unit = _job("sdk-tests-reusable.yaml", "unit")
    warm = _job("warm-caches.yaml", "unit-venvs")
    assert _matrix(warm) == _matrix(unit)


def test_warm_job_runs_each_leg_on_its_own_runner_and_python():
    warm = _job("warm-caches.yaml", "unit-venvs")
    assert warm["runs-on"] == "${{ matrix.os }}"
    setup = [
        s for s in warm["steps"] if s.get("uses") == "./.github/actions/setup-deps"
    ]
    assert len(setup) == 1, warm["steps"]
    assert setup[0]["with"]["python-version"] == "${{ matrix.python-version }}"
    assert setup[0]["with"]["lookup-only"] == "true"


def test_saved_venv_carries_bytecode():
    install = _setup_deps_step("Install dependencies")
    assert install["env"]["UV_COMPILE_BYTECODE"] == "1"


def test_venv_key_still_follows_the_lock_and_conformance_source():
    key = _setup_deps_step("Cache virtual environment")["with"]["key"]
    assert "hashFiles('uv.lock', 'packages/conformance/conformance/**')" in key
    assert "-pyc-" in key
