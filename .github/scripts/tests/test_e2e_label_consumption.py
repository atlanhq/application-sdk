"""Guards for consuming the `e2e` label once its run finishes (FND-3411).

The label used to be a standing setting: left on a PR or a release, every
later push re-ran the live-tenant suite, usually days after anyone wanted
it. Now the run that acted on the label removes it.

Three properties carry the design, and each is pinned here:

* The removal uses `github.token`, never the PAT. GitHub starts no workflow
  run for an event the run's own GITHUB_TOKEN caused, so the `unlabeled`
  event re-fires nothing — in particular not a consumer's Release Gate that
  predates the status check, which would otherwise go red.
* The consumer Tests Gate records the verdict as an `e2e` commit status
  before removing the label, because the label was the Release Gate's only
  evidence. Only a real verdict (success / failure) is recorded.
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

from _gha_expr import evaluate  # noqa: E402

_WORKFLOWS = Path(__file__).resolve().parents[3] / ".github/workflows"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8"))


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in job["steps"] if s.get("name") == name)


def _pr_event(labels: list[str], action: str = "synchronize", **extra: Any) -> dict:
    event: dict[str, Any] = {
        "action": action,
        "pull_request": {
            "labels": [{"name": n} for n in labels],
            "head": {"repo": {"fork": False}},
            "user": {"login": "someone"},
        },
    }
    event.update(extra)
    return {"event_name": "pull_request", "event": event}


# ── Consumer Tests Gate (tests-reusable.yaml) ─────────────────────────────────


@pytest.fixture(scope="module")
def gate_job() -> dict[str, Any]:
    return _load("tests-reusable.yaml")["jobs"]["tests-passed"]


def _needs(discover: str, e2e: str) -> dict[str, Any]:
    return {"discover-e2e": {"result": discover}, "e2e": {"result": e2e}}


@pytest.mark.parametrize(
    ("event_name", "discover", "e2e", "expected"),
    [
        ("pull_request", "success", "success", True),
        ("pull_request", "success", "failure", True),
        # No verdict: a skipped or cancelled matrix proves nothing either way.
        ("pull_request", "success", "skipped", False),
        ("pull_request", "success", "cancelled", False),
        ("pull_request", "skipped", "skipped", False),
        # Dispatched runs (the SDK's connector fan-out) have no PR head.
        ("workflow_dispatch", "success", "success", False),
    ],
)
def test_verdict_is_recorded_only_for_a_real_pr_verdict(
    gate_job: dict[str, Any],
    event_name: str,
    discover: str,
    e2e: str,
    expected: bool,
) -> None:
    step = _step(gate_job, "Record the e2e verdict on the head commit")
    contexts = {"github": {"event_name": event_name}, "needs": _needs(discover, e2e)}
    assert evaluate(step["if"], contexts) is expected


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
        ("workflow_dispatch", "success", False),
    ],
)
def test_label_is_removed_whenever_this_run_consumed_it(
    gate_job: dict[str, Any],
    event_name: str,
    discover: str,
    expected: bool,
) -> None:
    step = _step(gate_job, "Remove the e2e label")
    contexts = {
        "github": {"event_name": event_name},
        "needs": _needs(discover, "skipped"),
    }
    assert evaluate(step["if"], contexts) is expected


def test_verdict_lands_before_the_label_goes(gate_job: dict[str, Any]) -> None:
    """Between the two, a release PR has neither signal for its Release Gate."""
    names = [s.get("name") for s in gate_job["steps"]]
    assert names.index("Record the e2e verdict on the head commit") < names.index(
        "Remove the e2e label"
    )


def test_verdict_targets_the_pr_head(gate_job: dict[str, Any]) -> None:
    """`github.sha` is the merge ref; the Release Gate reads the head."""
    step = _step(gate_job, "Record the e2e verdict on the head commit")
    assert step["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert step["env"]["STATE"] == "${{ needs.e2e.result }}"
    assert "context=e2e" in step["run"]


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


@pytest.fixture(scope="module")
def consume_job() -> dict[str, Any]:
    return _load("pull_request.yaml")["jobs"]["consume-e2e-label"]


@pytest.mark.parametrize(
    ("github", "expected"),
    [
        (_pr_event(["e2e"], "synchronize"), True),
        (_pr_event(["e2e"], "labeled", label={"name": "e2e"}), True),
        # An unrelated label add did not request a run, so consumes nothing.
        (_pr_event(["e2e", "size/S"], "labeled", label={"name": "size/S"}), False),
        (_pr_event([], "synchronize"), False),
        ({"event_name": "merge_group", "event": {}}, False),
    ],
)
def test_sdk_job_fires_exactly_when_the_e2e_jobs_could(
    consume_job: dict[str, Any], github: dict[str, Any], expected: bool
) -> None:
    assert evaluate(consume_job["if"], {"github": github}) is expected


def test_sdk_job_waits_for_every_label_consuming_job(
    consume_job: dict[str, Any],
) -> None:
    """Removing before connector-gate settles would be removing mid-run."""
    workflow = _load("pull_request.yaml")
    consumers = {
        job_id
        for job_id, job in workflow["jobs"].items()
        if job_id != "consume-e2e-label"
        and "labels.*.name, 'e2e'" in str(job.get("if", ""))
    }
    assert consumers, "no label-gated jobs found; the sweep is broken"
    assert consumers | {"connector-gate"} <= set(consume_job["needs"])


def test_sdk_job_uses_github_token_and_fails_open(
    consume_job: dict[str, Any],
) -> None:
    step = _step(consume_job, "Remove the e2e label")
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["continue-on-error"] is True
    assert consume_job["permissions"] == {"pull-requests": "write"}
