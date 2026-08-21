"""Guards for the FND-354 cloud-narrowing wiring in both e2e reusables.

The behaviour being protected is that *editing `E2E_TENANT_MATRIX_JSON` is the
fleet-wide cloud-rotation lever*: removing a cloud narrows the fan-out on the
next run of every repo, with no connector PR and no SDK PR. That only holds if
discovery is actually handed the secret's key list — and the failure mode of
losing the wiring is not a red build. It is the pre-FND-354 behaviour: a
defaulted leg for a cloud the secret no longer carries, hard-failing in every
e2e-running repo, at the moment an operator reached for the lever to stop
exactly that.

Deliberately YAML-shape assertions: the wiring is GitHub Actions' own (step
outputs, skipped-step semantics, action inputs) and cannot be exercised without
a runner. The narrowing *logic* is unit-tested in test_discover_e2e_suites.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two reusables that fan an e2e run out over clouds, and the job in each
#: that decides the fan-out. Both must narrow, or the lever works in one
#: workflow and not the other — which is worse than not working at all, because
#: the operator cannot tell which they are looking at.
_WORKFLOWS = {
    ".github/workflows/tests-reusable.yaml": "discover-e2e",
    ".github/workflows/e2e-full-reusable.yaml": "plan-clouds",
}

_DISCOVER_ACTION = "atlanhq/application-sdk/.github/actions/discover-e2e-suites@main"
_KEYS_SCRIPT = "e2e_tenant_matrix_clouds.py"
_KEYS_STEP_ID = "matrix-clouds"
_AVAILABLE = "${{ steps.%s.outputs.clouds }}" % _KEYS_STEP_ID

#: A mention of the tenant-matrix secret, with `op` capturing the comparison
#: that makes it a presence test rather than a substitution of its value.
_SECRET_MENTION = re.compile(r"secrets\.E2E_TENANT_MATRIX_JSON\s*(?P<op>[=!]=)?")


@pytest.fixture(scope="module", params=sorted(_WORKFLOWS))
def steps(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    """Every step of the cloud-planning job, for each workflow in turn."""
    workflow = yaml.safe_load((_REPO_ROOT / request.param).read_text(encoding="utf-8"))
    return workflow["jobs"][_WORKFLOWS[request.param]]["steps"]


def _keys_step(steps: list[dict[str, Any]]) -> dict[str, Any]:
    matching = [s for s in steps if s.get("id") == _KEYS_STEP_ID]
    assert len(matching) == 1, (
        f"expected exactly one `id: {_KEYS_STEP_ID}` step reading the tenant "
        "matrix's cloud keys; without it the defaulted fan-out cannot narrow"
    )
    return matching[0]


def _discover_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matching = [s for s in steps if s.get("uses") == _DISCOVER_ACTION]
    assert matching, "the cloud-planning job must invoke the discovery action"
    return matching


def test_the_key_list_is_read_from_the_secret(steps: list[dict[str, Any]]) -> None:
    step = _keys_step(steps)
    assert _KEYS_SCRIPT in step["run"], (
        "the keys must come from the tested script, not from inline jq/python — "
        "branching in YAML cannot be regression-tested (docs/standards/ci.md)"
    )
    assert step["env"]["E2E_TENANT_MATRIX_JSON"] == (
        "${{ secrets.E2E_TENANT_MATRIX_JSON }}"
    )
    assert (
        '>> "$GITHUB_OUTPUT"' in step["run"]
    ), "the key list has to reach the discovery step as a step output"


def test_the_key_step_is_skipped_without_the_secret(
    steps: list[dict[str, Any]],
) -> None:
    # A skipped step's outputs are "", which discovery reads as "not known" and
    # narrows nothing. Running it unconditionally would instead hand discovery
    # an empty key list derived from an empty secret — same string, and today
    # that also narrows nothing, but the equivalence is incidental and the
    # `if:` is what keeps it from mattering.
    assert _keys_step(steps)["if"] == "env.HAS_TENANT_MATRIX != ''"


def test_every_discovery_invocation_is_narrowed(steps: list[dict[str, Any]]) -> None:
    # tests-reusable.yaml calls the action twice — the suite × cloud matrix and
    # the cloud-only matrix prepare-tenant installs from. A narrowing applied to
    # one and not the other means preparing a tenant for a cloud no leg runs on,
    # or worse, running legs on a cloud nothing installed to.
    for step in _discover_steps(steps):
        assert step["with"].get("available-clouds") == _AVAILABLE, (
            f"`{step.get('name', step['uses'])}` must pass the tenant matrix's "
            "cloud keys, or its defaulted fan-out still emits legs for clouds "
            "the secret no longer carries"
        )


def test_the_blob_never_reaches_discovery(steps: list[dict[str, Any]]) -> None:
    # The key list crosses this boundary; the credentials do not. Only
    # the per-leg tenant resolver needs them, and it renders exactly one cloud's
    # entry per leg — the least-privilege property FND-6 built in.
    #
    # Naming the secret to test its PRESENCE is fine and is what the `clouds`
    # expression already does; interpolating its VALUE is not. So every mention
    # must be a comparison, not a substitution.
    for step in _discover_steps(steps):
        for name, value in step["with"].items():
            for mention in _SECRET_MENTION.finditer(str(value)):
                assert mention.group("op"), (
                    f"`{name}` interpolates E2E_TENANT_MATRIX_JSON's VALUE into "
                    "discovery — pass the cloud key list instead; the payload "
                    "belongs to the per-leg tenant resolver, which renders one "
                    "cloud's entry per leg"
                )


def test_the_scripts_checkout_is_pinned_to_this_workflows_sha(
    steps: list[dict[str, Any]],
) -> None:
    # The script is executed, so its provenance is pinned to the workflow file
    # running it rather than to @main or to a caller-supplied ref. It is also
    # what makes this change testable on the branch that makes it: an action
    # pinned @main cannot see a new input until it lands.
    checkouts = [
        s
        for s in steps
        if str(s.get("uses", "")).startswith("actions/checkout@")
        and s.get("with", {}).get("repository") == "atlanhq/application-sdk"
    ]
    assert len(checkouts) == 1, (
        "the keys script has to be fetched from the SDK: the cloud-planning job "
        "checks out the CALLER's tree, which has no .github/scripts of ours"
    )
    with_ = checkouts[0]["with"]
    assert with_["ref"] == "${{ job.workflow_sha }}"
    assert with_["path"] == "application-sdk-scripts"
    assert with_["sparse-checkout"] == ".github/scripts"
    assert with_["persist-credentials"] is False
