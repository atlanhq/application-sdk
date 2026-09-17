"""Guards for the FND-1865 per-suite wiring in tests-reusable.yaml.

The e2e job has fanned out one leg per suite since FND-6, but two of the values
each leg runs with were resolved once for the whole repo: `source-available` and
the compose overlay. A multi-entrypoint connector whose entrypoints are not
equally testable cannot express that with one value each — db2's LUW flavour has
a community container, its z/OS flavour cannot have one at all — and the app-side
workarounds do not work (the harness's class attribute loses to
`E2E_SOURCE_AVAILABLE` on every CI run; a module-level `pytest.skip` exits 5,
which the composite propagates verbatim, so the leg reds instead of skipping).

Both are now resolved per suite in the discovery job and carried in the matrix.
The failure mode of losing that wiring is NOT a red build:

* an e2e leg that forwards `inputs.source-available` again silently puts every
  suite back on one boolean, and the sourceless leg goes back to burning the
  tenant-resolution phase before failing (or skipping from inside the suite);
* an e2e leg that pins the overlay path again silently starts every source
  container on every leg, and the worker's `depends_on: service_healthy` makes
  the legs that cannot use one wait for it;
* a discovery step that stops passing `compose-overlay` emits no
  `compose-overlay` key at all, so the leg forwards "" and the sdr-e2e action
  falls back to its OWN convention — the SDR overlay — on the full-DAG pipeline.

Deliberately YAML-shape assertions: the wiring is GitHub Actions' own (job
outputs, matrix contexts, action inputs) and cannot be exercised without a
runner. The resolution *logic* is unit-tested in test_discover_e2e_suites.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/tests-reusable.yaml"
_ACTION = _REPO_ROOT / ".github/actions/discover-e2e-suites/action.yaml"

_DISCOVER_ACTION = "atlanhq/application-sdk/.github/actions/discover-e2e-suites@main"
_SDR_E2E_ACTION = "atlanhq/application-sdk/.github/actions/sdr-e2e@main"

#: The overlay path the fleet's connectors actually carry, and the value the e2e
#: job used to pin at its sdr-e2e step. It now appears once, at the discovery
#: step, as the fallback the per-suite convention is derived from.
_REPO_WIDE_OVERLAY = ".github/e2e/e2e-full-docker-compose.yaml"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def action() -> dict[str, Any]:
    return yaml.safe_load(_ACTION.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def discover_step(workflow: dict[str, Any]) -> dict[str, Any]:
    steps = workflow["jobs"]["discover-e2e"]["steps"]
    matching = [
        s
        for s in steps
        if s.get("uses") == _DISCOVER_ACTION
        and s.get("with", {}).get("clouds-only") != "true"
    ]
    assert len(matching) == 1, (
        "expected exactly one suite-mode discovery step; the cloud-only call is "
        "prepare-tenant's and carries no suite dimension to key these off"
    )
    return matching[0]


@pytest.fixture(scope="module")
def e2e_step(workflow: dict[str, Any]) -> dict[str, Any]:
    steps = workflow["jobs"]["e2e"]["steps"]
    matching = [s for s in steps if s.get("uses") == _SDR_E2E_ACTION]
    assert len(matching) == 1, "the e2e job must invoke the sdr-e2e composite once"
    return matching[0]


def test_the_e2e_matrix_is_the_discovery_job_s(workflow: dict[str, Any]) -> None:
    # Everything below rests on this: the per-suite values are resolved in
    # discovery, so the legs must be expanded from the matrix it emitted.
    matrix = workflow["jobs"]["e2e"]["strategy"]["matrix"]
    assert "needs.discover-e2e.outputs.matrix" in matrix


def test_discovery_is_given_both_per_suite_dimensions(
    discover_step: dict[str, Any],
) -> None:
    with_ = discover_step["with"]
    assert with_["source-available"] == "${{ inputs.source-available }}"
    assert (
        with_["source-available-overrides"]
        == "${{ inputs.source-available-overrides }}"
    ), (
        "the caller's per-suite overrides have to reach discovery; nothing else "
        "in the run can see the suite names they key off"
    )
    assert with_["compose-overlay"] == _REPO_WIDE_OVERLAY, (
        "discovery needs the repo-wide overlay as the fallback AND as the "
        "directory the per-suite convention is derived from; without it no "
        "compose-overlay key is emitted and the leg forwards an empty path"
    )


def test_the_leg_forwards_what_discovery_resolved(e2e_step: dict[str, Any]) -> None:
    with_ = e2e_step["with"]
    assert with_["source-available"] == "${{ matrix.source-available }}", (
        "forwarding inputs.source-available again puts every suite back on one "
        "repo-wide boolean, silently"
    )
    assert with_["compose-overlay"] == "${{ matrix.compose-overlay }}", (
        "pinning the path again silently starts every source container on " "every leg"
    )


def test_the_overlay_path_is_written_once(workflow: dict[str, Any]) -> None:
    # Two spellings of the fallback would drift: discovery would key the
    # convention off one directory while the legs fell back to a file in
    # another. The fallback belongs at the resolution site.
    text = _WORKFLOW.read_text(encoding="utf-8")
    occurrences = [line for line in text.splitlines() if _REPO_WIDE_OVERLAY in line]
    assert len(occurrences) == 1, (
        f"{_REPO_WIDE_OVERLAY} appears {len(occurrences)} times; it is the "
        "discovery step's fallback input and nothing else's: "
        f"{occurrences}"
    )
    assert workflow["jobs"]["discover-e2e"], "sanity: the discovery job still exists"


def test_the_caller_input_exists_and_is_optional(workflow: dict[str, Any]) -> None:
    # Optional and empty-by-default: every existing caller keeps the repo-wide
    # behaviour without an edit.
    spec = workflow[True]["workflow_call"]["inputs"]["source-available-overrides"]
    assert spec["required"] is False
    assert spec["type"] == "string"
    assert spec["default"] == ""


def test_the_action_declares_the_inputs_the_workflow_passes(
    action: dict[str, Any], discover_step: dict[str, Any]
) -> None:
    # An input the action does not declare is a warning on the run and nothing
    # else: the value is dropped and every leg quietly keeps the default.
    declared = action["inputs"]
    for name in ("source-available", "source-available-overrides", "compose-overlay"):
        assert name in declared, f"the discovery action must declare `{name}`"
        assert declared[name]["required"] is False
    assert declared["source-available"]["default"] == "true"
    assert declared["source-available-overrides"]["default"] == ""
    assert declared["compose-overlay"]["default"] == ""
    assert set(discover_step["with"]) <= set(
        declared
    ), "the discovery step passes an input the action does not declare"
