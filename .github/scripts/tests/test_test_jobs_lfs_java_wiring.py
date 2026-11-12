"""Guards for the opt-in ``test-lfs`` and ``java-version`` inputs of tests-reusable.

Apps whose unit or integration tests load an LFS-tracked jar and start a JVM
(a vendored SQL parser) need both in the Unit and Integration jobs. Both must
be off by default so every existing caller runs exactly as before, and
``test-lfs`` must stay separate from ``lfs``, which only reaches the e2e image
build. Asserted as YAML shape: the behaviour is GitHub Actions' own and cannot
run without a runner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
ACTIONS = REPO_ROOT / ".github" / "actions"
REUSABLE = REPO_ROOT / ".github" / "workflows" / "tests-reusable.yaml"

JAVA_STEP_NAME = "Set up Java (java-version)"
# job -> the composite it runs the tests through
TEST_JOBS = {
    "unit": "connector-unit-tests",
    "integration": "connector-integration-tests",
}


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    return _load(REUSABLE)


def test_inputs_are_declared_and_off_by_default(workflow: dict[Any, Any]) -> None:
    inputs = workflow[True]["workflow_call"]["inputs"]  # `on` parses as True
    assert inputs["test-lfs"]["type"] == "boolean"
    assert inputs["test-lfs"]["default"] is False
    assert inputs["java-version"]["type"] == "string"
    assert inputs["java-version"]["default"] == ""


def _composite_step(job: dict[str, Any], action: str) -> dict[str, Any]:
    steps = [s for s in job["steps"] if f"/actions/{action}@" in s.get("uses", "")]
    assert len(steps) == 1, f"expected one {action} step"
    return steps[0]


@pytest.mark.parametrize("job_name,action", TEST_JOBS.items())
def test_test_jobs_forward_test_lfs_not_lfs(
    workflow: dict[str, Any],
    job_name: str,
    action: str,
) -> None:
    step = _composite_step(workflow["jobs"][job_name], action)
    assert step["with"]["lfs"] == "${{ inputs.test-lfs }}", (
        f"{job_name}: the test checkout must follow test-lfs; `lfs` is the e2e "
        "image build's and existing callers already set it"
    )


@pytest.mark.parametrize("job_name,action", TEST_JOBS.items())
def test_java_is_set_up_only_on_opt_in_and_before_the_tests(
    workflow: dict[str, Any],
    job_name: str,
    action: str,
) -> None:
    job = workflow["jobs"][job_name]
    java = [s for s in job["steps"] if s.get("name") == JAVA_STEP_NAME]
    assert len(java) == 1, f"{job_name}: missing '{JAVA_STEP_NAME}'"
    step = java[0]
    assert step["if"] == "inputs.java-version != ''"
    assert step["uses"].startswith("actions/setup-java@")
    sha = step["uses"].split("@", 1)[1]
    assert len(sha) == 40, "setup-java must be pinned by full commit SHA"
    assert step["with"] == {
        "distribution": "temurin",
        "java-version": "${{ inputs.java-version }}",
    }
    assert job["steps"].index(step) < job["steps"].index(
        _composite_step(job, action)
    ), f"{job_name}: Java must be on PATH before the tests run"


@pytest.mark.parametrize("action", TEST_JOBS.values())
def test_composites_pass_lfs_to_setup_deps_default_off(action: str) -> None:
    meta = _load(ACTIONS / action / "action.yaml")
    assert meta["inputs"]["lfs"]["default"] == "false"
    deps = [
        s for s in meta["runs"]["steps"] if "/actions/setup-deps@" in s.get("uses", "")
    ]
    assert len(deps) == 1
    assert deps[0]["with"]["lfs"] == "${{ inputs.lfs }}"


def test_setup_deps_checks_out_lfs_only_on_opt_in() -> None:
    meta = _load(ACTIONS / "setup-deps" / "action.yaml")
    assert meta["inputs"]["lfs"]["default"] == "false"
    checkouts = [
        s
        for s in meta["runs"]["steps"]
        if str(s.get("uses", "")).startswith("actions/checkout@")
    ]
    by_if = {s["if"]: s for s in checkouts}
    assert set(by_if) == {"inputs.lfs != 'true'", "inputs.lfs == 'true'"}, (
        "exactly one checkout must run for any lfs value"
    )

    default = by_if["inputs.lfs != 'true'"]
    assert default["with"] == {"fetch-depth": 1}, "default checkout must not change"
    assert "env" not in default, "default checkout must keep the job's smudge skip"

    opt_in = by_if["inputs.lfs == 'true'"]
    assert opt_in["with"] == {"fetch-depth": 1, "lfs": True}
    # Without this, a job-level GIT_LFS_SKIP_SMUDGE=1 (git-lfs-skip-smudge)
    # leaves pointers in the tree even though checkout fetched the objects.
    assert opt_in["env"] == {"GIT_LFS_SKIP_SMUDGE": "0"}
