"""Guard for the FND-110 apt-packages remediation in the conformance pipeline.

The D series is the FOURTH runner-sync site in the fleet.  #3060 wired
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
Actions' own and cannot be exercised without a runner.  The package-name check
lives in install_apt_packages.py (executed by test_install_apt_packages.py);
here both of the suite's install steps — the ``apt-packages`` input and the
``.github/ci-system-deps.txt`` file the bootstrap writes — are pinned to call
it, and the D series to wait on both (FND-4908).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _conformance_series import load_workflow, series, suite_job  # noqa: E402
from _gha_expr import evaluate  # noqa: E402

APT_STEP_NAME = "Install system packages (apt-packages)"


@pytest.fixture(scope="module")
def workflow() -> dict:  # type: ignore[type-arg]
    return load_workflow()


@pytest.fixture(scope="module")
def suite(workflow: dict) -> dict:  # type: ignore[type-arg]
    return suite_job(workflow)


@pytest.fixture(scope="module")
def d_detect(suite: dict) -> dict:  # type: ignore[type-arg]
    """The one detect step that syncs the caller's environment."""
    synced = [entry for entry in series(suite) if entry.needs_env]
    assert len(synced) == 1, f"expected one env-syncing series, got {synced}"
    return synced[0].detect


def test_apt_packages_input_is_declared_and_off_by_default(workflow: dict) -> None:  # type: ignore[type-arg]
    inputs = workflow[True]["workflow_call"]["inputs"]  # `on` parses as True
    assert "apt-packages" in inputs, (
        "apt-packages input missing from conformance-reusable"
    )
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


def test_apt_step_precedes_the_d_series_detect_step(
    suite: dict, d_detect: dict
) -> None:  # type: ignore[type-arg]
    """A post-detect install would be dead code for the failure it fixes: the
    D series' `uv sync` (and its native-sdist build) happens inside the detect
    action, so the packages must be on the runner before that step fires."""
    steps = suite["steps"]
    apt_idx = steps.index(_apt_step(suite))
    run_idx = steps.index(d_detect)
    assert apt_idx < run_idx, "apt step must run before the D-series detect step"

    names = [s.get("name") for s in steps]
    assert "Set up uv" in names[:apt_idx], (
        "apt step must sit after 'Set up uv' so the ordering requirement the "
        "task pins is explicit"
    )


def _contexts(
    *, apt: str, event: str, force_all: bool, dependency: str, apt_outcome: str
) -> dict:  # type: ignore[type-arg]
    return {
        "inputs": {
            "apt-packages": apt,
            "event_name": event,
            "force-all": force_all,
        },
        "steps": {
            "changes": {"outputs": {"dependency": dependency}},
            "apt-packages": {"outcome": apt_outcome},
        },
    }


_SCENARIOS = [
    (event, force_all, dependency)
    for event in ("pull_request", "merge_group", "push")
    for force_all in (False, True)
    for dependency in ("true", "false", "")
]


@pytest.mark.parametrize("event,force_all,dependency", _SCENARIOS)
def test_apt_step_fires_exactly_when_the_d_series_does(
    suite: dict,
    d_detect: dict,
    event: str,
    force_all: bool,
    dependency: str,  # type: ignore[type-arg]
) -> None:
    """The guards live in `if:`, never in the shell (docs/standards/ci.md), and
    the install fires under exactly the conditions the D-series detect fires
    under — evaluated, not string-matched, so a reworded gate that still means
    the same thing passes and one that drifted does not.  An empty input is a
    no-op whatever else is true."""
    apt_if = _apt_step(suite)["if"]
    d_if = d_detect["if"]

    with_input = _contexts(
        apt="libkrb5-dev",
        event=event,
        force_all=force_all,
        dependency=dependency,
        apt_outcome="success",
    )
    assert evaluate(apt_if, with_input) == evaluate(d_if, with_input), (
        "apt step and D-series detect disagree on whether D is relevant — the "
        "install would fire for a series that then no-ops, or be missing for "
        "one that syncs"
    )

    without_input = _contexts(
        apt="",
        event=event,
        force_all=force_all,
        dependency=dependency,
        apt_outcome="skipped",
    )
    assert not evaluate(apt_if, without_input), "empty apt-packages must self-skip"


def test_d_series_does_not_run_after_a_failed_install(d_detect: dict) -> None:  # type: ignore[type-arg]
    """A failed install means `uv sync` fails too, further from the cause."""
    contexts = _contexts(
        apt="libkrb5-dev",
        event="push",
        force_all=False,
        dependency="",
        apt_outcome="failure",
    )
    assert not evaluate(d_detect["if"], contexts)


def test_apt_step_routes_the_value_through_env(suite: dict) -> None:  # type: ignore[type-arg]
    """docs/standards/ci.md: caller-controlled values reach `run:` via env,
    never by direct ``${{ }}`` interpolation into the script text."""
    step = _apt_step(suite)
    assert step.get("env", {}).get("APT_PACKAGES") == "${{ inputs.apt-packages }}"
    assert "${{" not in step["run"], (
        "apt step interpolates an expression into the shell script"
    )


def test_only_the_d_series_materialises_the_environment(suite: dict) -> None:  # type: ignore[type-arg]
    """The apt gate is only meaningful if exactly one series syncs.  The
    others run detect from an isolated `uvx` env."""
    needs_env = [entry.letter for entry in series(suite) if entry.needs_env]
    assert needs_env == ["D"], (
        f"expected only the D series to set needs-env, got {needs_env}"
    )


# ---------------------------------------------------------------------------
# Both install steps go through install_apt_packages.py (FND-4908).
# ---------------------------------------------------------------------------

SCRIPT = '"$RUNNER_TEMP/install_apt_packages.py"'
FILE_STEP_ID = "system-deps-file"
DEPS_FILE = ".github/ci-system-deps.txt"


Step = dict[str, Any]


def _steps(job: dict) -> list[Step]:  # type: ignore[type-arg]
    steps: list[Step] = job["steps"]
    return steps


def _step_id(job: dict, step_id: str) -> Step:  # type: ignore[type-arg]
    matches = [s for s in _steps(job) if s.get("id") == step_id]
    assert len(matches) == 1, f"expected one step with id {step_id!r}"
    return matches[0]


def test_apt_step_runs_the_shared_script(suite: dict) -> None:  # type: ignore[type-arg]
    assert _apt_step(suite)["run"].strip() == f"python3 {SCRIPT} --env APT_PACKAGES"


def test_the_script_is_fetched_and_kept_past_the_tree_cleanup(suite: dict) -> None:  # type: ignore[type-arg]
    """Fetched with the lock-check scripts at this workflow's own SHA, copied
    out of the tree before that checkout is removed (so no series scans it),
    unconditionally: the file step below needs it without any input set."""
    steps = _steps(suite)
    fetch = next(
        i
        for i, s in enumerate(steps)
        if str(s.get("with", {}).get("path")) == ".sdk-lock-check"
    )
    fetch_with: dict[str, str] = steps[fetch]["with"]
    assert fetch_with["ref"] == "${{ job.workflow_sha }}"
    assert ".github/scripts/install_apt_packages.py" in (
        fetch_with["sparse-checkout"].split()
    )
    keep = next(
        i
        for i, s in enumerate(steps)
        if str(s.get("run", "")).startswith(
            "cp .sdk-lock-check/.github/scripts/install_apt_packages.py "
        )
    )
    assert "if" not in steps[keep]
    assert str(steps[keep]["run"]).endswith('"$RUNNER_TEMP/install_apt_packages.py"')
    remove = next(
        i for i, s in enumerate(steps) if s.get("run") == "rm -rf .sdk-lock-check"
    )
    assert fetch < keep < remove < steps.index(_apt_step(suite))


def test_the_declared_file_is_installed_through_the_script(
    suite: dict, d_detect: dict
) -> None:  # type: ignore[type-arg]
    """The bootstrap's .github/ci-system-deps.txt used to reach apt-get through
    `xargs` in the vendored detect action, unchecked.  It now goes through the
    script here, before D, for every repo that has the file."""
    step = _step_id(suite, FILE_STEP_ID)
    assert step["run"].strip() == f"python3 {SCRIPT} --file {DEPS_FILE}"
    assert f"hashFiles('{DEPS_FILE}') != ''" in step["if"]
    assert "private-git-deps" not in step["if"], (
        "the file applies to every repo that declares it, not only ones with "
        "private git deps"
    )
    steps = _steps(suite)
    assert steps.index(_apt_step(suite)) < steps.index(step) < steps.index(d_detect)


@pytest.mark.parametrize("event,force_all,dependency", _SCENARIOS)
def test_file_step_follows_the_d_series_relevance_gate(
    suite: dict,
    d_detect: dict,
    event: str,
    force_all: bool,
    dependency: str,
) -> None:
    """Evaluated with the `hashFiles` term (which the evaluator does not
    model) taken as true: the rest of the gate must match D's."""
    gate = _step_id(suite, FILE_STEP_ID)["if"].replace(
        f"&& hashFiles('{DEPS_FILE}') != ''", ""
    )
    assert "hashFiles" not in gate
    contexts = _contexts(
        apt="",
        event=event,
        force_all=force_all,
        dependency=dependency,
        apt_outcome="skipped",
    )
    assert evaluate(gate, contexts) == evaluate(d_detect["if"], contexts)


@pytest.mark.parametrize("step_id", ["git-deps-presync", "detect-dependency"])
def test_nothing_syncs_after_a_failed_file_install(suite: dict, step_id: str) -> None:  # type: ignore[type-arg]
    """A rejected or failed install means `uv sync` fails too, further from
    the cause."""
    contexts = _contexts(
        apt="", event="push", force_all=False, dependency="", apt_outcome="skipped"
    )
    contexts["inputs"]["private-git-deps"] = True
    contexts["steps"][FILE_STEP_ID] = {"outcome": "failure"}
    contexts["steps"]["git-deps-presync"] = {"outcome": "skipped"}
    assert not evaluate(_step_id(suite, step_id)["if"], contexts)
