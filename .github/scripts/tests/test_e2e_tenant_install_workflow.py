"""Guards for .github/workflows/e2e-tenant-install.yaml.

The workflow takes free-text `workflow_dispatch` inputs and installs an app onto
a live tenant, so the shell-injection surface is the thing worth pinning: an
input interpolated directly into a `run:` block is spliced into the script before
bash ever sees a quote, so a crafted value executes instead of being compared.
Routing every input through `env:` and referencing a quoted ``"$VAR"`` makes bash
treat it as data.

Scoped deliberately to this one workflow. A repo-wide sweep currently finds ~49
pre-existing instances of the same pattern across unrelated workflows; asserting
on all of them here would fail immediately on work this change does not own. That
backlog is worth its own ticket, not a guard that has to be born disabled.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/e2e-tenant-install.yaml"

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
        "splices attacker-influenced text into the shell. Pass it via env: and "
        f'reference a quoted "$VAR" instead. Offenders: {offenders}'
    )


def test_inputs_used_by_scripts_arrive_through_env(workflow: dict) -> None:
    """Every input the install/verify steps consume must be exported via env:."""
    by_name = {str(s.get("name", "")): s for s in _steps(workflow)}
    expectations = {
        "Install onto the tenant": {
            "APP_ID",
            "IMAGE",
            "VERSION",
            "BRANCH",
            # GM rejects a version-create without `repo` for any app with a
            # source_repo on file, and LM does not expose the registered value.
            "REPO_URL",
            "SCAN_WAIT_SECONDS",
            "INSTALL_RETRY_SECONDS",
            "TIMEOUT_SECONDS",
        },
        "Verify the tenant reports the installed version": {"APP_ID", "VERSION"},
    }
    for step_name, required in expectations.items():
        step = by_name.get(step_name)
        assert step is not None, f"step {step_name!r} is gone; update this guard"
        env = set(step.get("env") or {})
        missing = required - env
        assert not missing, f"{step_name} no longer exports {sorted(missing)} via env:"


def test_install_requires_explicit_confirmation(workflow: dict) -> None:  # type: ignore[type-arg]
    """Installing onto a tenant is a deployment; it must not run by default."""
    triggers = workflow.get("on", workflow.get(True))
    confirm = triggers["workflow_dispatch"]["inputs"]["confirm"]
    assert confirm["default"] == "no"
    assert set(confirm["options"]) == {"no", "yes"}
    assert workflow["jobs"]["install"]["if"] == "inputs.confirm == 'yes'", (
        "the confirmation gate must be a job-level if:, so an unconfirmed run is "
        "visibly skipped rather than green-but-did-nothing"
    )


def test_the_ghcr_check_can_actually_read_packages(workflow: dict) -> None:  # type: ignore[type-arg]
    """The pre-publish image check needs a token with package scope.

    `docker manifest inspect` runs against GHCR, and the login falls back to
    `github.token` when ORG_PAT_GITHUB is unset. The workflow declares only
    `contents: read` at the top level, so without a job-level grant that token has
    no package scope at all and the check 401s on an image that exists — a
    guardrail that false-blocks a legitimate install.

    Necessary, not sufficient: GITHUB_TOKEN package access covers only packages
    owned by or linked to this repo, and these images belong to the app repos. The
    step's error text names authorisation as well as absence for that reason, which
    is asserted here too — a false block that reads as "the tag was pruned" sends
    the next person hunting the wrong cause, which is exactly how one live run was
    spent.
    """
    permissions = workflow["jobs"]["install"].get("permissions") or {}
    assert permissions.get("packages") == "read", (
        "the install job needs `packages: read`, or the github.token fallback "
        "cannot read GHCR and the image check blocks installs of images that exist"
    )
    # The top-level grant must survive: a job-level block replaces it wholesale.
    assert permissions.get("contents") == "read"

    check = [
        s
        for s in _steps(workflow)
        if str(s.get("name", "")) == "Verify the image exists"
    ]
    assert len(check) == 1
    error = check[0]["run"]
    assert "ORG_PAT_GITHUB" in error, (
        "the failure message must name the authorisation cause, not only the "
        "pruned-tag one — a cross-repo GHCR read fails here even with the grant"
    )


def test_job_timeout_stays_above_the_two_waits_it_defaults_to(workflow: dict) -> None:  # type: ignore[type-arg]
    """The runner's timeout must not be able to fire before the script's.

    Both waits are dispatch inputs here, so the budget is read off their own
    defaults rather than the script's: raising a default without raising the job
    timeout would turn a slow LM sync into a bare "job cancelled", discarding the
    actionable error the script was about to print.
    """
    inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    waits = (
        int(inputs["install_retry_seconds"]["default"])
        + int(inputs["timeout_seconds"]["default"])
    ) // 60
    # Same 1/2 share as prepare-tenant: both waits can run to completion and the
    # rest of the job still has as long again, so the runner's timeout is never the
    # first to fire.
    required = round(waits / 0.5)
    actual = workflow["jobs"]["install"]["timeout-minutes"]
    assert actual >= required, (
        f"the install job allows {actual} min but its own input defaults permit "
        f"{waits} min of waiting. Raise it to at least {required}."
    )


def test_concurrency_is_per_cloud_and_does_not_cancel(workflow: dict) -> None:  # type: ignore[type-arg]
    concurrency = workflow["concurrency"]
    assert "inputs.cloud" in concurrency["group"]
    # Cancelling mid-install leaves the tenant in whatever state LM reached.
    assert concurrency["cancel-in-progress"] is False
