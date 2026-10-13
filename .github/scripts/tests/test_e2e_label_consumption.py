"""Guards for recording the e2e verdict and consuming the `e2e` label (FND-3411).

The label used to be a standing setting: left on a PR or a release, every
later push re-ran the live-tenant suite, usually days after anyone wanted
it. So the run that acted on the label removed it.

FND-3839 replaced the label with a `/e2e` PR comment, which dispatches the
connector's tests.yaml on the head commit: a one-shot request by
construction, with nothing to consume. Label consumption survives only for
callers that predate the resync and still subscribe to `labeled`
(`e2e-label-trigger: true`), and application-sdk's own PR Checks no longer
reads the label at all, so its consume job is gone.

The properties pinned here:

* The consumer Tests Gate records the verdict as an `e2e` commit status —
  the Tests Gate hold and the Release Gate read it. A real verdict is
  recorded as success / failure; a run that acted on a request without one
  records `error`, replacing the `pending` its discover-e2e job posted
  (FND-3650). That holds for a labelled PR run AND for a connector-local
  dispatch (no `application-sdk-ref`), which owns the commit it tested; the
  SDK's cross-repo dispatch tests this repo's main against an SDK commit and
  writes nothing here.
* The label removal (legacy callers only) uses `github.token`, never the
  PAT. GitHub starts no workflow run for an event the run's own GITHUB_TOKEN
  caused, so the `unlabeled` event re-fires nothing.
* Every step is fail-open. The Tests Gate job IS the required check; a 404
  (label already gone) must not redden it.

The `if:` gates are lifted verbatim from the YAML and evaluated, because a
textual check proves presence but not precedence.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate, evaluate_operand  # noqa: E402

_WORKFLOWS = Path(__file__).resolve().parents[3] / ".github/workflows"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8"))


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in job["steps"] if s.get("name") == name)


# ── Consumer Tests Gate (tests-reusable.yaml) ─────────────────────────────────


@pytest.fixture(scope="module")
def gate_job() -> dict[str, Any]:
    return _load("tests-reusable.yaml")["jobs"]["tests-passed"]


def _needs(discover: str, e2e: str) -> dict[str, Any]:
    return {"discover-e2e": {"result": discover}, "e2e": {"result": e2e}}


@pytest.mark.parametrize(
    ("event_name", "sdk_ref", "discover", "e2e", "expected"),
    [
        ("pull_request", "", "success", "success", True),
        ("pull_request", "", "success", "failure", True),
        # Acted on the request but produced no verdict: still recorded (as
        # `error`), so the `pending` discover-e2e posted never lingers.
        ("pull_request", "", "success", "skipped", True),
        ("pull_request", "", "success", "cancelled", True),
        ("pull_request", "", "failure", "skipped", True),
        # Did not act on a request: this run owns no verdict.
        ("pull_request", "", "skipped", "skipped", False),
        # A `/e2e` or `/gate-check` dispatch on the connector's own branch
        # owns the verdict for the commit it tested (FND-3839)…
        ("workflow_dispatch", "", "success", "success", True),
        ("workflow_dispatch", "", "success", "cancelled", True),
        # …unless it stood e2e down (the branch moved past the commented-on
        # commit), in which case it tested nothing.
        ("workflow_dispatch", "", "skipped", "skipped", False),
        # The SDK's cross-repo fan-out tests main against an SDK commit; a
        # verdict on this repo's commit would be about the wrong change.
        ("workflow_dispatch", "abc123", "success", "success", False),
    ],
)
def test_verdict_is_recorded_whenever_this_run_acted_on_a_request(
    gate_job: dict[str, Any],
    event_name: str,
    sdk_ref: str,
    discover: str,
    e2e: str,
    expected: bool,
) -> None:
    step = _step(gate_job, "Record the e2e verdict on the head commit")
    contexts = {
        "github": {"event_name": event_name},
        "inputs": {"application-sdk-ref": sdk_ref},
        "needs": _needs(discover, e2e),
    }
    assert evaluate(step["if"], contexts) is expected


@pytest.mark.parametrize(
    ("e2e", "state"),
    [
        ("success", "success"),
        ("failure", "failure"),
        # No verdict proves nothing; `error` blocks like a failure.
        ("skipped", "error"),
        ("cancelled", "error"),
    ],
)
def test_only_a_real_verdict_is_recorded_as_one(
    gate_job: dict[str, Any], e2e: str, state: str
) -> None:
    env = _step(gate_job, "Record the e2e verdict on the head commit")["env"]
    contexts = {"needs": _needs("success", e2e)}
    assert evaluate_operand(env["STATE"], contexts) == state
    description = evaluate_operand(env["DESCRIPTION"], contexts)
    assert len(description) <= 140  # the statuses API limit


@pytest.mark.parametrize(
    ("event_name", "discover", "expected"),
    [
        ("pull_request", "success", True),
        # Discovery ran but found nothing / failed: the request was still
        # consumed, and retrying is re-adding the label.
        ("pull_request", "failure", True),
        ("pull_request", "cancelled", True),
        # Not requested on this event — an unrelated label add, or no label.
        ("pull_request", "skipped", False),
        # A dispatch was requested by a comment; there is no label to consume.
        ("workflow_dispatch", "success", False),
    ],
)
def test_a_legacy_caller_removes_the_label_whenever_this_run_consumed_it(
    gate_job: dict[str, Any],
    event_name: str,
    discover: str,
    expected: bool,
) -> None:
    """Callers that predate FND-3839 still request e2e with the label."""
    step = _step(gate_job, "Remove the e2e label")
    contexts = {
        "github": {"event_name": event_name},
        "inputs": {"e2e-label-trigger": True},
        "needs": _needs(discover, "skipped"),
    }
    assert evaluate(step["if"], contexts) is expected


@pytest.mark.parametrize("discover", ["success", "failure", "skipped"])
def test_the_resynced_scaffold_never_touches_labels(
    gate_job: dict[str, Any], discover: str
) -> None:
    """With `e2e-label-trigger: false` a label requested nothing, so removing
    one would strip a label someone added for another reason."""
    step = _step(gate_job, "Remove the e2e label")
    contexts = {
        "github": {"event_name": "pull_request"},
        "inputs": {"e2e-label-trigger": False},
        "needs": _needs(discover, "skipped"),
    }
    assert evaluate(step["if"], contexts) is False


def test_verdict_lands_before_the_label_goes(gate_job: dict[str, Any]) -> None:
    """Between the two, a release PR has neither signal for its Release Gate."""
    names = [s.get("name") for s in gate_job["steps"]]
    assert names.index("Record the e2e verdict on the head commit") < names.index(
        "Remove the e2e label"
    )


@pytest.mark.parametrize(
    ("github", "sha"),
    [
        # `github.sha` is the merge ref on a PR run; the Release Gate reads
        # the head.
        (
            {
                "event_name": "pull_request",
                "sha": "merge-ref",
                "event": {"pull_request": {"head": {"sha": "pr-head"}}},
            },
            "pr-head",
        ),
        # A dispatch on the PR branch checks out its tip, which is the
        # commented-on head (discover-e2e stands down otherwise).
        (
            {"event_name": "workflow_dispatch", "sha": "branch-tip", "event": {}},
            "branch-tip",
        ),
    ],
    ids=["pull_request", "workflow_dispatch"],
)
def test_verdict_targets_the_tested_head(
    gate_job: dict[str, Any], github: dict[str, Any], sha: str
) -> None:
    step = _step(gate_job, "Record the e2e verdict on the head commit")
    assert evaluate_operand(step["env"]["HEAD_SHA"], {"github": github}) == sha
    assert "record_e2e_verdict.py" in step["run"]


def test_gate_job_can_write_the_status(gate_job: dict[str, Any]) -> None:
    assert gate_job["permissions"]["statuses"] == "write"
    assert gate_job["permissions"]["pull-requests"] == "write"


@pytest.mark.parametrize(
    "name", ["Record the e2e verdict on the head commit", "Remove the e2e label"]
)
def test_gate_steps_use_github_token_and_fail_open(
    gate_job: dict[str, Any], name: str
) -> None:
    step = _step(gate_job, name)
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["continue-on-error"] is True


def test_gate_steps_precede_enforcement(gate_job: dict[str, Any]) -> None:
    """After `Enforce gate` fails the job, later steps would never run."""
    names = [s.get("name") for s in gate_job["steps"]]
    assert names.index("Remove the e2e label") < names.index("Enforce gate")


# ── application-sdk's own PR Checks (pull_request.yaml) ──────────────────────


def test_sdk_pr_checks_reads_no_labels_so_has_nothing_to_consume() -> None:
    """e2e there follows the `e2e-request` commit status (FND-3839).

    The consume job existed only because a job gated on the `e2e` label would
    re-fire on every later push while the label stayed. With no job reading a
    label, a stale label is inert — and a returning label read would bring
    back that re-fire with nothing left to stop it.
    """
    workflow = _load("pull_request.yaml")
    assert "consume-e2e-label" not in workflow["jobs"]
    readers = [
        job_id
        for job_id, job in workflow["jobs"].items()
        if "labels.*.name" in str(job.get("if", ""))
        or "github.event.label" in str(job.get("if", ""))
    ]
    assert not readers, f"these PR Checks jobs read PR labels again: {readers}"
