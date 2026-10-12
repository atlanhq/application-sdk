"""Tests for .github/scripts/e2e_commit_verdict.py and its wiring (FND-3650).

A run started by an unrelated label skips e2e (FND-48) and, as the newest check
suite on the commit, used to green the required Tests Gate over the run that was
actually running e2e, or over one whose e2e had failed. These tests pin both
layers of the fix:

* the driver's decision table, with ``gh``, the clock and ``sleep`` stubbed so
  waiting is exercised without waiting;
* the workflow wiring, by evaluating the real ``if:`` and env expressions lifted
  from ``tests-reusable.yaml`` against the scenarios the issue lists. A correct
  driver behind a gate that never reaches it fixes nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import e2e_commit_verdict  # noqa: E402
from e2e_commit_verdict import (  # noqa: E402
    StatusUnreadable,
    decide,
    main,
    read_e2e_state,
)
from rerun_deferred_gate import DEFER_STEP_NAME  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate, evaluate_operand  # noqa: E402

REPO = "atlanhq/atlan-example-app"
SHA = "a96a27b0000000000000000000000000000000aa"

_REUSABLE = (
    Path(__file__).resolve().parents[3] / ".github/workflows/tests-reusable.yaml"
)


_RUN = 38060382427


def _row(
    context: str, state: str, run_id: int | None = _RUN, status_id: int = 0
) -> dict:
    url = (
        f"https://github.com/{REPO}/actions/runs/{run_id}"
        if run_id is not None
        else "https://example.invalid/elsewhere"
    )
    return {"id": status_id, "context": context, "state": state, "target_url": url}


def _status_payload(*rows: tuple, page_size: int = 100) -> str:
    """The `--paginate --slurp` listing: a list of pages, newest status first.

    Each row is ``(context, state)`` or ``(context, state, run_id)``, given
    newest first; status ids are assigned in that order.
    """
    built = [
        _row(row[0], row[1], row[2] if len(row) > 2 else _RUN, len(rows) - index)
        for index, row in enumerate(rows)
    ]
    pages = [built[i : i + page_size] for i in range(0, len(built), page_size)] or [[]]
    return json.dumps(pages)


class _Clock:
    """A fake monotonic clock that ``sleep`` advances."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


def _sequence(*responses: str):
    """A ``run`` double returning each response in turn, then repeating the last."""
    remaining = list(responses)

    def run(args: list) -> str:
        assert args[1] == f"repos/{REPO}/commits/{SHA}/statuses?per_page=100"
        assert "--paginate" in args and "--slurp" in args
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return run


def _decide(run, *, awaiting: bool = False, clock: _Clock | None = None):
    clock = clock or _Clock()
    return decide(
        REPO,
        SHA,
        awaiting_label_run=awaiting,
        run=run,
        clock=clock,
        sleep=clock.sleep,
        wait_budget=600,
        settle_budget=120,
        poll_interval=20,
    )


# --- reading the status ------------------------------------------------------


def test_reads_the_e2e_context_only() -> None:
    run = _sequence(_status_payload(("ci/other", "failure"), ("e2e", "success")))
    assert read_e2e_state(REPO, SHA, run) == "success"


def test_no_e2e_context_is_an_empty_state() -> None:
    run = _sequence(_status_payload(("ci/other", "failure")))
    assert read_e2e_state(REPO, SHA, run) == ""


@pytest.mark.parametrize("raw", ["", "not json", "{}", '[{"statuses": []}]'])
def test_an_unreadable_payload_raises_rather_than_reading_as_absent(raw: str) -> None:
    """Absent passes the gate, so an API failure must never be spelled that way."""
    with pytest.raises(StatusUnreadable):
        read_e2e_state(REPO, SHA, _sequence(raw))


@pytest.mark.parametrize("state", [None, 7, "", "neutral"])
def test_an_unrecognised_e2e_state_raises_rather_than_reading_as_absent(
    state: object,
) -> None:
    raw = json.dumps([[{**_row("e2e", "success"), "state": state, "id": 1}]])
    with pytest.raises(StatusUnreadable):
        read_e2e_state(REPO, SHA, _sequence(raw))


def test_an_e2e_status_past_the_first_page_is_found() -> None:
    rows = [("ci/other", "success")] * 150 + [("e2e", "failure")]
    run = _sequence(_status_payload(*rows))
    assert read_e2e_state(REPO, SHA, run) == "failure"


def test_an_attempts_latest_state_wins() -> None:
    run = _sequence(_status_payload(("e2e", "failure"), ("e2e", "pending")))
    assert read_e2e_state(REPO, SHA, run) == "failure"


def test_a_late_older_success_cannot_mask_a_newer_pending_attempt() -> None:
    """The label re-added mid-run: the older attempt finishes after the newer
    one has posted `pending`. Its success is the newest row, but not the verdict."""
    run = _sequence(
        _status_payload(
            ("e2e", "success", _RUN),
            ("e2e", "pending", _RUN + 5),
            ("e2e", "pending", _RUN),
        )
    )
    assert read_e2e_state(REPO, SHA, run) == "pending"


def test_a_newer_attempts_verdict_replaces_an_older_failure() -> None:
    """Re-adding the label is how a failure is retried."""
    run = _sequence(
        _status_payload(
            ("e2e", "success", _RUN + 5),
            ("e2e", "pending", _RUN + 5),
            ("e2e", "failure", _RUN),
        )
    )
    assert read_e2e_state(REPO, SHA, run) == "success"


def test_a_status_naming_no_run_never_outranks_a_real_attempt() -> None:
    run = _sequence(_status_payload(("e2e", "success", None), ("e2e", "failure", _RUN)))
    assert read_e2e_state(REPO, SHA, run) == "failure"


# --- the decision table ------------------------------------------------------


def test_a_passed_e2e_on_the_commit_passes() -> None:
    passed, row, _ = _decide(_sequence(_status_payload(("e2e", "success"))))
    assert passed
    assert row.startswith("✅")


@pytest.mark.parametrize("state", ["failure", "error"])
@pytest.mark.parametrize("awaiting", [True, False])
def test_a_failed_e2e_blocks_whether_or_not_the_label_is_still_there(
    state: str, awaiting: bool
) -> None:
    """The issue's must-block case: e2e fails, then an unrelated label lands.

    ``awaiting=False`` is the same case after FND-3411 consumed the label: the
    payload no longer carries ``e2e``, but the status still does.
    """
    passed, _, reason = _decide(
        _sequence(_status_payload(("e2e", state))), awaiting=awaiting
    )
    assert not passed
    assert state in reason


def test_no_e2e_on_the_commit_passes_when_none_was_requested() -> None:
    """The normal PR: e2e is required only for releases."""
    clock = _Clock()
    passed, _, _ = _decide(_sequence(_status_payload()), clock=clock)
    assert passed
    assert clock.sleeps == 0


def test_a_pending_e2e_is_waited_on_until_it_passes() -> None:
    """An unrelated label added while e2e runs: the gate stays pending, then greens."""
    clock = _Clock()
    run = _sequence(
        _status_payload(("e2e", "pending")),
        _status_payload(("e2e", "pending")),
        _status_payload(("e2e", "success")),
    )
    passed, _, _ = _decide(run, clock=clock)
    assert passed
    assert clock.sleeps == 2


def test_a_pending_e2e_that_then_fails_blocks() -> None:
    run = _sequence(
        _status_payload(("e2e", "pending")), _status_payload(("e2e", "failure"))
    )
    passed, _, _ = _decide(run)
    assert not passed


def test_a_pending_e2e_that_outlasts_the_budget_fails_rather_than_passes() -> None:
    clock = _Clock()
    passed, row, reason = _decide(
        _sequence(_status_payload(("e2e", "pending"))), clock=clock
    )
    assert not passed
    assert row.startswith("⏳")
    assert "re-run" in reason
    assert clock.now >= 600


def test_awaiting_a_label_run_waits_for_its_pending_to_appear() -> None:
    """The acting sibling may not have posted `pending` when this gate reads."""
    run = _sequence(
        _status_payload(),
        _status_payload(("e2e", "pending")),
        _status_payload(("e2e", "success")),
    )
    passed, _, _ = _decide(run, awaiting=True)
    assert passed


def test_awaiting_a_label_run_that_never_reports_fails_after_the_settle_window() -> (
    None
):
    clock = _Clock()
    passed, _, reason = _decide(
        _sequence(_status_payload()), awaiting=True, clock=clock
    )
    assert not passed
    assert "`e2e` label" in reason
    assert 120 <= clock.now < 600


def test_an_unreadable_status_is_retried_then_fails_closed() -> None:
    clock = _Clock()
    passed, _, _ = _decide(_sequence(""), clock=clock)
    assert not passed
    assert clock.now >= 600


def test_a_transient_read_error_recovers() -> None:
    passed, _, _ = _decide(_sequence("", _status_payload(("e2e", "success"))))
    assert passed


def test_main_emits_outputs_on_stdout_and_annotations_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        e2e_commit_verdict, "_run_gh", _sequence(_status_payload(("e2e", "failure")))
    )
    assert (
        main(["--repo", REPO, "--head-sha", SHA, "--awaiting-label-run", "true"]) == 0
    )
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert "passed=false" in lines
    assert any(line.startswith("e2e-status=") for line in lines)
    assert all("=" in line for line in lines)
    assert "::error::" in err


# --- workflow wiring ---------------------------------------------------------


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(_REUSABLE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def gate_job(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow["jobs"]["tests-passed"]


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in job["steps"] if s.get("name") == name)


def _hold(gate_job: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in gate_job["steps"] if s.get("id") == "e2e-verdict")


def _pr(labels: list[str], action: str, label: str | None = None, **pr: Any) -> dict:
    event: dict[str, Any] = {
        "action": action,
        "pull_request": {
            "labels": [{"name": n} for n in labels],
            "head": {"repo": {"fork": pr.get("fork", False)}},
            "user": {"login": pr.get("author", "someone")},
        },
    }
    if label is not None:
        event["label"] = {"name": label}
    return {"event_name": "pull_request", "event": event}


def _discover_runs(workflow: dict[str, Any], github: dict) -> bool:
    return evaluate(
        workflow["jobs"]["discover-e2e"]["if"],
        {"github": github, "inputs": {"enable-e2e": True, "run-e2e": "false"}},
    )


def _hold_runs(
    gate_job: dict[str, Any], github: dict, *, discover: str, gate_passed: str
) -> bool:
    return evaluate(
        _hold(gate_job)["if"],
        {
            "github": github,
            "inputs": {"enable-e2e": True},
            "needs": {"discover-e2e": {"result": discover}},
            "steps": {"gate": {"outputs": {"passed": gate_passed}}},
        },
    )


def _awaiting(gate_job: dict[str, Any], github: dict) -> Any:
    return evaluate_operand(
        _hold(gate_job)["env"]["AWAITING_LABEL_RUN"], {"github": github}
    )


# Each scenario: the event, whether this run acts on e2e, and — when it does
# not — whether its gate holds on the commit's verdict and whether it is
# awaiting an acting sibling.
_SCENARIOS = {
    "unrelated label after e2e": (
        _pr(["e2e", "mothership-reviewed"], "labeled", "mothership-reviewed"),
        False,
        True,
    ),
    "e2e label added (the acting run)": (_pr(["e2e"], "labeled", "e2e"), True, None),
    "push with the label (re-triggers e2e)": (_pr(["e2e"], "synchronize"), True, None),
    "label consumed, then an unrelated label": (
        _pr(["size/S"], "labeled", "size/S"),
        False,
        False,
    ),
    "label removed": (_pr([], "unlabeled", "e2e"), False, False),
    "plain push, no label": (_pr([], "synchronize"), False, False),
    "dependabot with the label never runs e2e": (
        _pr(["e2e", "deps"], "labeled", "deps", author="dependabot[bot]"),
        False,
        False,
    ),
    "fork with the label never runs e2e": (
        _pr(["e2e", "deps"], "labeled", "deps", fork=True),
        False,
        False,
    ),
}


@pytest.mark.parametrize("name", list(_SCENARIOS))
def test_every_non_acting_run_holds_and_the_acting_run_does_not(
    workflow: dict[str, Any], gate_job: dict[str, Any], name: str
) -> None:
    github, acts, awaiting = _SCENARIOS[name]
    assert _discover_runs(workflow, github) is acts
    discover = "success" if acts else "skipped"
    assert (
        _hold_runs(gate_job, github, discover=discover, gate_passed="true") is not acts
    )
    if not acts:
        assert _awaiting(gate_job, github) is awaiting


def test_a_failed_gate_does_not_wait_on_e2e(gate_job: dict[str, Any]) -> None:
    """It already fails; holding it would only delay the red."""
    github = _pr(["e2e", "x"], "labeled", "x")
    assert not _hold_runs(gate_job, github, discover="skipped", gate_passed="false")


def test_the_hold_is_off_when_e2e_is_disabled(gate_job: dict[str, Any]) -> None:
    contexts = {
        "github": _pr(["e2e", "x"], "labeled", "x"),
        "inputs": {"enable-e2e": False},
        "needs": {"discover-e2e": {"result": "skipped"}},
        "steps": {"gate": {"outputs": {"passed": "true"}}},
    }
    assert not evaluate(_hold(gate_job)["if"], contexts)


def test_the_hold_is_pr_only(gate_job: dict[str, Any]) -> None:
    contexts = {
        "github": {"event_name": "merge_group", "event": {}},
        "inputs": {"enable-e2e": True},
        "needs": {"discover-e2e": {"result": "skipped"}},
        "steps": {"gate": {"outputs": {"passed": "true"}}},
    }
    assert not evaluate(_hold(gate_job)["if"], contexts)


def test_the_hold_reads_the_pr_head_with_the_driver(gate_job: dict[str, Any]) -> None:
    hold = _hold(gate_job)
    assert hold["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert "e2e_commit_verdict.py" in hold["run"]
    assert '>> "$GITHUB_OUTPUT"' in hold["run"]
    # Must not be fail-open: a crash has to reach the enforce step as a failure.
    assert not hold.get("continue-on-error")


def test_the_hold_fits_inside_the_job_timeout(gate_job: dict[str, Any]) -> None:
    assert gate_job["timeout-minutes"] > _hold(gate_job)["timeout-minutes"]
    budget_minutes = e2e_commit_verdict._WAIT_BUDGET_SECONDS / 60
    assert _hold(gate_job)["timeout-minutes"] > budget_minutes


@pytest.mark.parametrize(
    ("conclusion", "passed", "enforced"),
    [
        ("skipped", "", False),
        ("success", "true", False),
        ("success", "false", True),
        # Crashed, timed out, or the driver was never checked out: no output.
        ("failure", "", True),
    ],
)
def test_the_enforce_step_fails_closed(
    gate_job: dict[str, Any], conclusion: str, passed: str, enforced: bool
) -> None:
    step = _step(gate_job, DEFER_STEP_NAME)
    contexts = {
        "steps": {
            "e2e-verdict": {"conclusion": conclusion, "outputs": {"passed": passed}}
        }
    }
    assert evaluate(step["if"], contexts) is enforced
    assert step["run"].strip() == "exit 1"


def test_the_driver_is_checked_out_before_the_hold(gate_job: dict[str, Any]) -> None:
    names = [s.get("name") for s in gate_job["steps"]]
    checkout = names.index("Check out the gate's helper drivers")
    assert checkout < gate_job["steps"].index(_hold(gate_job))
    assert names.index("Re-run an evicted Tests run on this commit") > checkout


def test_the_summary_reflects_the_held_verdict(gate_job: dict[str, Any]) -> None:
    message = _step(gate_job, "Post unified test-summary comment on PR")["with"][
        "message"
    ]
    assert "steps.e2e-verdict.outputs.e2e-status" in message
    assert "steps.e2e-verdict.outputs.passed" in message


def test_the_acting_run_marks_the_verdict_pending_first(
    workflow: dict[str, Any],
) -> None:
    steps = workflow["jobs"]["discover-e2e"]["steps"]
    names = [s.get("name") for s in steps]
    index = names.index("Mark the e2e verdict pending on the head commit")
    # Ahead of the first step that costs time, so a sibling sees it in seconds.
    first_uses = next(i for i, s in enumerate(steps) if s.get("uses") is not None)
    assert index < first_uses
    step = steps[index]
    assert "state=pending" in step["run"]
    assert "context=e2e" in step["run"]
    assert step["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert evaluate(step["if"], {"github": {"event_name": "pull_request"}})
    assert not evaluate(step["if"], {"github": {"event_name": "workflow_dispatch"}})
