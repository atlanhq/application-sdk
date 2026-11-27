"""warm-caches.yaml must warm exactly the venvs the SDK unit legs restore.

A unit leg can only restore a venv saved on its own ref or on main, and only
warm-caches.yaml saves on main for most of the matrix. GitHub Actions cannot
share a ``strategy.matrix`` across workflows, so the unit job's OS x Python
list is repeated in warm-caches.yaml; a leg added to one and not the other
silently goes back to starting cold (FND-4309).
"""

from __future__ import annotations

from _workflows import ACTIONS, job, load_workflow, step_named

SETUP_DEPS = ACTIONS / "setup-deps" / "action.yaml"


def _matrix(job: dict) -> dict[str, list[str]]:
    matrix = job["strategy"]["matrix"]
    return {axis: sorted(matrix[axis]) for axis in ("os", "python-version")}


def test_warm_matrix_equals_unit_matrix():
    unit = job(load_workflow("sdk-tests-reusable.yaml"), "unit")
    warm = job(load_workflow("warm-caches.yaml"), "unit-venvs")
    assert _matrix(warm) == _matrix(unit)


def test_warm_job_runs_each_leg_on_its_own_runner_and_python():
    warm = job(load_workflow("warm-caches.yaml"), "unit-venvs")
    assert warm["runs-on"] == "${{ matrix.os }}"
    setup = [
        s for s in warm["steps"] if s.get("uses") == "./.github/actions/setup-deps"
    ]
    assert len(setup) == 1, warm["steps"]
    assert setup[0]["with"]["python-version"] == "${{ matrix.python-version }}"
    assert setup[0]["with"]["lookup-only"] == "true"


def test_saved_venv_carries_bytecode():
    install = step_named(load_workflow(SETUP_DEPS), "Install dependencies")
    assert install["env"]["UV_COMPILE_BYTECODE"] == "1"


def test_venv_key_follows_the_lock():
    # The conformance package is an editable workspace member (FND-4671), so
    # .venv never holds a built copy of its source to go stale.
    key = step_named(load_workflow(SETUP_DEPS), "Cache virtual environment")["with"][
        "key"
    ]
    assert "hashFiles('uv.lock') }}" in key
    assert "-pyc-" in key
