"""Guards for .github/workflows/e2e-tenant-uninstall.yaml.

FND-709's sweep half: the manual workflow that clears the version pins already
sitting on the e2e tenants, and the standing escape hatch for one the automatic
cleanup could not clear.

Same two properties the install workflow's guards protect, for the same reasons.
It takes free-text `workflow_dispatch` inputs and REMOVES apps from a live
tenant, so (a) no input may be interpolated into a `run:` block, where it would be
spliced into the script before bash sees a quote, and (b) it must not be able to
run without an explicit confirmation.

Plus one this workflow needs and the install one does not: the `clouds` fan-out is
built in the expression layer so the `run:` blocks stay straight-line
(docs/standards/ci.md), and an expression that renders wrong is a sweep pointed at
the wrong tenants — so it is evaluated here rather than trusted.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))

from _gha_expr import evaluate_operand  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/e2e-tenant-uninstall.yaml"

#: Any `${{ inputs.* }}` reference, which is what must not appear inside `run:`.
_INPUT_REF = re.compile(r"\$\{\{\s*inputs\.[A-Za-z0-9_-]+\s*\}\}")


@pytest.fixture(scope="module")
def workflow() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _steps(workflow: dict) -> list[dict]:  # type: ignore[type-arg]
    steps: list[dict] = []  # type: ignore[type-arg]
    for job in workflow["jobs"].values():
        steps.extend(job.get("steps") or [])
    return steps


def _dispatch_inputs(workflow: dict) -> dict:  # type: ignore[type-arg]
    # `workflow[True]`: YAML 1.1 resolves the bare key `on` to the boolean true.
    triggers = workflow.get("on", workflow.get(True))
    return triggers["workflow_dispatch"]["inputs"]


def test_no_dispatch_input_is_interpolated_into_a_run_block(workflow: dict) -> None:  # type: ignore[type-arg]
    offenders: list[str] = []
    for step in _steps(workflow):
        script = step.get("run")
        if not isinstance(script, str):
            continue
        for match in _INPUT_REF.findall(script):
            offenders.append(f"{step.get('name', '?')}: {match}")
    assert not offenders, (
        "these run: blocks interpolate a workflow_dispatch input directly, which "
        "splices attacker-influenced text into the shell. `app_ids` is a list of "
        "ids that reach a request path, so this is the one input where a crafted "
        f'value is worth real effort. Pass it via env: as a quoted "$VAR". '
        f"Offenders: {offenders}"
    )


def test_the_app_id_list_arrives_through_env(workflow: dict) -> None:  # type: ignore[type-arg]
    by_name = {str(s.get("name", "")): s for s in _steps(workflow)}
    step = by_name.get("Uninstall the apps")
    assert step is not None, "the uninstall step is gone; update this guard"
    env = set(step.get("env") or {})
    assert {"APP_IDS", "TIMEOUT_SECONDS"} <= env
    # The credentials are NOT re-exported here: the resolver already wrote them to
    # $GITHUB_ENV, and every hop a secret takes is a hop it can be mishandled on.
    assert not {"SDR_CLIENT_ID", "SDR_CLIENT_SECRET", "ATLAN_API_KEY"} & env


def test_uninstall_requires_explicit_confirmation(workflow: dict) -> None:  # type: ignore[type-arg]
    """Removing an app from a tenant is a deployment; it must not run by default."""
    confirm = _dispatch_inputs(workflow)["confirm"]
    assert confirm["default"] == "no"
    assert set(confirm["options"]) == {"no", "yes"}
    assert workflow["jobs"]["uninstall"]["if"] == "inputs.confirm == 'yes'", (
        "the confirmation gate must be a job-level if:, so an unconfirmed run is "
        "visibly skipped rather than green-but-did-nothing"
    )


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ("all", ["aws", "azure", "gcp"]),
        ("aws", ["aws"]),
        ("azure", ["azure"]),
        ("gcp", ["gcp"]),
    ],
)
def test_the_cloud_fan_out_renders_for_every_option(
    workflow: dict,  # type: ignore[type-arg]
    selection: str,
    expected: list[str],
) -> None:
    """A matrix expression that renders wrong points the sweep at the wrong
    tenants — or at none, which is a green run that did nothing. Evaluated for
    every option the `choice` offers rather than spot-checked, because "all" and
    the single-cloud form take different arms of the `&&`/`||`.
    """
    matrix = workflow["jobs"]["uninstall"]["strategy"]["matrix"]["cloud"]
    inner = matrix.strip()
    assert inner.startswith("${{ fromJson(") and inner.endswith(") }}")
    # fromJson is not modelled by the evaluator, so the argument is evaluated and
    # parsed here — which is exactly what fromJson does.
    argument = inner[len("${{ fromJson(") : -len(") }}")]
    rendered = evaluate_operand(argument, {"inputs": {"clouds": selection}})
    assert json.loads(rendered) == expected

    # And the option list and the fan-out must not drift apart: a cloud added to
    # the choice but not to the "all" arm would be silently skipped by every sweep.
    options = set(_dispatch_inputs(workflow)["clouds"]["options"]) - {"all"}
    all_arm = json.loads(evaluate_operand(argument, {"inputs": {"clouds": "all"}}))
    assert set(all_arm) == options


def test_one_wedged_tenant_does_not_stop_the_others(workflow: dict) -> None:  # type: ignore[type-arg]
    """The pins accumulated on all three tenants, so a sweep that abandons two
    clouds because the third is wedged leaves most of the hazard in place."""
    assert workflow["jobs"]["uninstall"]["strategy"]["fail-fast"] is False


def test_concurrency_is_per_cloud_selection_and_does_not_cancel(workflow: dict) -> None:  # type: ignore[type-arg]
    concurrency = workflow["concurrency"]
    assert "inputs.clouds" in concurrency["group"]
    # Cancelling mid-uninstall leaves the tenant with the HelmRelease gone and the
    # install record still there.
    assert concurrency["cancel-in-progress"] is False


def test_residue_reds_this_run(workflow: dict) -> None:  # type: ignore[type-arg]
    """The opposite of the automatic cleanup's contract, deliberately.

    A pin that would not clear is precisely what this run was fired to fix, so it
    has to be a red run rather than a warning in a log nobody opens — which means
    NOT carrying the `continue-on-error` the e2e cleanup step does.
    """
    by_name = {str(s.get("name", "")): s for s in _steps(workflow)}
    assert "continue-on-error" not in by_name["Uninstall the apps"]
