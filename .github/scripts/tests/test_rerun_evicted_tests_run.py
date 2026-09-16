"""Tests for .github/scripts/rerun_evicted_tests_run.py (FND-2167).

`gh` is stubbed through the module's single `run` seam (docs/standards/ci.md), so
every decision — repair, leave alone, wait, fail open — is exercised with no
network access.

Two layers, because each catches what the other cannot:

* the behavioural tests below pin the decision table, including the two cases
  that must NOT act (a deliberate cancel, a run already re-run once);
* :func:`test_gate_job_wires_the_repair` onwards read `tests-reusable.yaml` and
  pin the wiring, because a correct driver that no workflow invokes — or one
  invoked from the evicted run instead of the run that produced a verdict —
  repairs nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from rerun_evicted_tests_run import (  # noqa: E402
    is_repairable_eviction,
    newer_runs,
    repair,
    rerun,
    run_duration_seconds,
    select_candidate,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate  # noqa: E402

REPO = "atlanhq/atlan-example-app"
SHA = "c55c9281dd792bda9601a3b1bcd936276ff7baa6"
WORKFLOW_ID = 4242
SELF = 35108818527

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REUSABLE = _REPO_ROOT / ".github/workflows/tests-reusable.yaml"


def _run(
    run_id: int,
    *,
    conclusion: str = "cancelled",
    status: str = "completed",
    attempt: int = 1,
    created: str = "2026-09-16T14:28:56Z",
    updated: str = "2026-09-16T14:29:23Z",
    workflow_id: int = WORKFLOW_ID,
) -> dict:
    """A workflow-run payload shaped like the real listing entry."""
    return {
        "id": run_id,
        "workflow_id": workflow_id,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "created_at": created,
        "run_started_at": created,
        "updated_at": updated,
    }


def _stub(*, listing: list, self_run: dict | None = None, reruns: list | None = None):
    """A `run` double serving the self-run read, the listing and the re-run POST."""
    calls = reruns if reruns is not None else []

    def run(args: list) -> str:
        path = args[-1] if args[1] == "-X" else args[1]
        if "/rerun" in path:
            calls.append(path)
            return "{}"
        if f"/actions/runs/{SELF}" in path:
            payload = self_run if self_run is not None else {"workflow_id": WORKFLOW_ID}
            return json.dumps(payload)
        return json.dumps([{"workflow_runs": listing}])

    return run


# --- duration parsing ------------------------------------------------------


def test_duration_prefers_run_started_at() -> None:
    assert (
        run_duration_seconds(
            {
                "run_started_at": "2026-09-16T14:28:56Z",
                "created_at": "2026-09-16T14:00:00Z",
                "updated_at": "2026-09-16T14:29:23Z",
            }
        )
        == 27
    )


def test_duration_falls_back_to_created_at_for_a_run_that_never_started() -> None:
    """An eviction never gets a runner, so `run_started_at` can be absent."""
    assert (
        run_duration_seconds(
            {"created_at": "2026-09-16T14:28:56Z", "updated_at": "2026-09-16T14:29:23Z"}
        )
        == 27
    )


@pytest.mark.parametrize(
    "run",
    [
        {},
        {"created_at": "not-a-date", "updated_at": "2026-09-16T14:29:23Z"},
        {"created_at": "2026-09-16T14:28:56Z"},
        {"created_at": None, "updated_at": None},
    ],
)
def test_duration_is_none_when_it_cannot_be_derived(run: dict) -> None:
    assert run_duration_seconds(run) is None


# --- the decision table ----------------------------------------------------


def test_a_short_cancelled_run_is_repairable() -> None:
    repairable, reason = is_repairable_eviction(_run(1), 180)
    assert repairable
    assert "signature of an eviction" in reason


def test_a_long_cancelled_run_reads_as_a_deliberate_cancel() -> None:
    """A person watching a run cancels it minutes in, and that must stick."""
    run = _run(1, created="2026-09-16T14:00:00Z", updated="2026-09-16T14:20:00Z")
    repairable, reason = is_repairable_eviction(run, 180)
    assert not repairable
    assert "deliberate cancel" in reason


def test_a_second_attempt_is_never_re_run_again() -> None:
    repairable, reason = is_repairable_eviction(_run(1, attempt=2), 180)
    assert not repairable
    assert "attempt 2" in reason


@pytest.mark.parametrize("conclusion", ["success", "failure", "timed_out", None])
def test_a_run_with_a_real_verdict_is_left_alone(conclusion: str | None) -> None:
    repairable, reason = is_repairable_eviction(_run(1, conclusion=conclusion), 180)
    assert not repairable
    assert "owns a real verdict" in reason


@pytest.mark.parametrize("status", ["in_progress", "queued", "waiting"])
def test_an_unfinished_run_is_left_to_post_its_own_verdict(status: str) -> None:
    repairable, reason = is_repairable_eviction(
        _run(1, status=status, conclusion=None), 180
    )
    assert not repairable
    assert "own verdict" in reason


def test_a_non_integer_attempt_is_treated_as_already_re_run() -> None:
    """Fail towards doing nothing when the payload is not the shape we expect."""
    repairable, _ = is_repairable_eviction(_run(1, attempt="2"), 180)  # type: ignore[arg-type]
    assert not repairable


# --- listing and filtering -------------------------------------------------


def test_only_newer_runs_of_the_same_workflow_are_candidates() -> None:
    listing = [
        _run(SELF - 1),  # older: its red row is harmless
        _run(SELF),  # self
        _run(SELF + 1),  # the one that owns the required context
        _run(SELF + 2, workflow_id=99),  # a different workflow on the same commit
        {"id": None},  # malformed entries are ignored, not crashes
    ]
    found = newer_runs(REPO, SHA, WORKFLOW_ID, SELF, _stub(listing=listing))
    assert [entry["id"] for entry in found or []] == [SELF + 1]


def test_pagination_pages_are_flattened() -> None:
    def run(_args: list) -> str:
        return json.dumps(
            [{"workflow_runs": [_run(SELF + 1)]}, {"workflow_runs": [_run(SELF + 2)]}]
        )

    found = newer_runs(REPO, SHA, WORKFLOW_ID, SELF, run)
    assert sorted(entry["id"] for entry in found or []) == [SELF + 1, SELF + 2]


@pytest.mark.parametrize("payload", ["", "not json", "{}", '{"message":"Not Found"}'])
def test_an_unreadable_listing_is_none_not_empty(payload: str) -> None:
    """ "Could not read" must never be mistaken for "there are no newer runs"."""
    assert newer_runs(REPO, SHA, WORKFLOW_ID, SELF, lambda _a: payload) is None


# --- candidate selection ---------------------------------------------------


def test_the_newest_evicted_run_is_selected() -> None:
    listing = [_run(SELF), _run(SELF + 1), _run(SELF + 2)]
    candidate = select_candidate(
        REPO, SHA, WORKFLOW_ID, SELF, run=_stub(listing=listing)
    )
    assert candidate is not None
    assert candidate["id"] == SELF + 2


def test_nothing_is_selected_when_this_run_is_the_newest() -> None:
    listing = [_run(SELF - 1), _run(SELF)]
    assert (
        select_candidate(REPO, SHA, WORKFLOW_ID, SELF, run=_stub(listing=listing))
        is None
    )


def test_an_evicted_run_under_a_newer_genuine_run_is_left_alone() -> None:
    """Only the newest suite owns the context, so only the newest is repaired."""
    listing = [_run(SELF + 1), _run(SELF + 2, conclusion="success")]
    assert (
        select_candidate(REPO, SHA, WORKFLOW_ID, SELF, run=_stub(listing=listing))
        is None
    )


def test_an_unreadable_listing_selects_nothing() -> None:
    assert select_candidate(REPO, SHA, WORKFLOW_ID, SELF, run=lambda _a: "") is None


def test_a_settling_run_is_waited_out_then_repaired() -> None:
    """The gate of a fast run can arrive mid-eviction; that state is undecidable.

    The autouse clock fixture makes `time.sleep` advance `monotonic` without
    waiting, so this exercises the real poll loop and the real budget.
    """
    states = iter(
        [
            [_run(SELF + 1, status="in_progress", conclusion=None)],
            [_run(SELF + 1, status="in_progress", conclusion=None)],
            [_run(SELF + 1)],
        ]
    )

    def run(args: list) -> str:
        if f"/actions/runs/{SELF}" in args[1]:
            return json.dumps({"workflow_id": WORKFLOW_ID})
        return json.dumps([{"workflow_runs": next(states)}])

    candidate = select_candidate(REPO, SHA, WORKFLOW_ID, SELF, run=run)
    assert candidate is not None and candidate["id"] == SELF + 1


def test_a_genuinely_running_newer_run_outlasts_the_budget() -> None:
    listing = [_run(SELF + 1, status="in_progress", conclusion=None)]
    assert (
        select_candidate(
            REPO, SHA, WORKFLOW_ID, SELF, run=_stub(listing=listing), settle_budget=30
        )
        is None
    )


# --- the re-run call -------------------------------------------------------


def test_rerun_posts_to_the_rerun_endpoint() -> None:
    calls: list = []

    def run(args: list) -> str:
        calls.append(args)
        return "{}"

    assert rerun(REPO, 7, run) is True
    assert calls == [["api", "-X", "POST", f"repos/{REPO}/actions/runs/7/rerun"]]


def test_dry_run_posts_nothing() -> None:
    calls: list = []

    def run(args: list) -> str:
        calls.append(args)
        return "{}"

    assert rerun(REPO, 7, run, dry_run=True) is True
    assert calls == []


def test_a_rejected_rerun_warns_and_names_the_token(capsys) -> None:
    assert rerun(REPO, 7, lambda _a: "") is False
    assert "actions: write" in capsys.readouterr().err


# --- end to end ------------------------------------------------------------


def test_repair_re_runs_the_newest_evicted_sibling() -> None:
    reruns: list = []
    listing = [_run(SELF), _run(SELF + 1), _run(SELF + 2)]
    assert repair(REPO, SHA, SELF, run=_stub(listing=listing, reruns=reruns)) is True
    assert reruns == [f"repos/{REPO}/actions/runs/{SELF + 2}/rerun"]


def test_repair_is_a_no_op_when_the_self_run_cannot_be_read(capsys) -> None:
    reruns: list = []
    assert (
        repair(REPO, SHA, SELF, run=_stub(listing=[], self_run={}, reruns=reruns))
        is False
    )
    assert reruns == []
    assert "which workflow it belongs to" in capsys.readouterr().err


def test_repair_never_raises_when_every_call_fails() -> None:
    assert repair(REPO, SHA, SELF, run=lambda _a: "") is False


# --- wiring in tests-reusable.yaml ----------------------------------------


def _gate_job() -> dict[str, Any]:
    workflow = yaml.safe_load(_REUSABLE.read_text(encoding="utf-8"))
    job = (workflow.get("jobs") or {}).get("tests-passed")
    assert job, "the `tests-passed` job is gone from tests-reusable.yaml"
    return job


def _repair_step() -> dict[str, Any]:
    steps = _gate_job().get("steps") or []
    matches = [
        step
        for step in steps
        if "rerun_evicted_tests_run.py" in str(step.get("run", ""))
    ]
    assert len(matches) == 1, (
        "the Tests Gate job must invoke the evicted-run repair exactly once; "
        f"found {len(matches)} call sites"
    )
    return matches[0]


def test_gate_job_wires_the_repair() -> None:
    step = _repair_step()
    assert step.get("continue-on-error") is True, (
        "the repair must never fail the gate job: it runs inside the required "
        "`tests / Tests Gate` context, so an API hiccup here would turn a "
        "cosmetic flake into the block it exists to remove"
    )
    env = step.get("env") or {}
    # Event-derived values go through env, never inline ${{ }} in the script
    # body (docs/standards/ci.md): `${{ }}` is substituted before bash parses,
    # so a ref carrying `$(...)` would execute.
    assert env.get("HEAD_SHA") == "${{ github.event.pull_request.head.sha }}"
    assert env.get("SELF_RUN_ID") == "${{ github.run_id }}"
    assert "ORG_PAT_GITHUB" in str(env.get("GH_TOKEN")), (
        "re-running needs `actions: write`, which only the PAT carries — the "
        "reusable's GITHUB_TOKEN cannot be raised to it without a hard workflow "
        "error in every consumer"
    )
    for name in ("HEAD_SHA", "SELF_RUN_ID", "REPO"):
        assert f'"${name}"' in str(step["run"]), (
            f"{name} must reach the script as a quoted shell variable, not as an "
            "interpolated expression"
        )


def test_the_script_the_gate_invokes_exists() -> None:
    assert (_REPO_ROOT / ".github/scripts/rerun_evicted_tests_run.py").is_file()


@pytest.mark.parametrize(
    "event_name,action,conclusion,expected",
    [
        # The run that produced a verdict repairs the newest evicted sibling —
        # whichever event started it, including a `labeled` one.
        ("pull_request", "opened", "success", True),
        ("pull_request", "synchronize", "failure", True),
        ("pull_request", "labeled", "success", True),
        # An evicted run must NOT act. It has no verdict to justify re-running
        # anything, and letting evicted runs re-run each other would put them
        # straight back into one concurrency group to evict each other again.
        ("pull_request", "opened", "cancelled", False),
        # Off the PR path the groups are run-unique, so nothing is ever evicted.
        ("merge_group", None, "success", False),
        ("push", None, "success", False),
    ],
)
def test_only_a_run_with_a_verdict_on_a_pull_request_repairs(
    event_name: str, action: str | None, conclusion: str, expected: bool
) -> None:
    expression = str(_repair_step()["if"])
    context = {
        "github": {"event_name": event_name, "event": {"action": action}},
        "steps": {"gate": {"outputs": {"conclusion": conclusion}}},
    }
    assert evaluate(expression, context) is expected


def test_the_gate_job_still_always_reports() -> None:
    """The load-bearing property the repair is built around.

    `tests-passed` must keep `if: always()`. A called workflow whose job is
    skipped publishes a check run with conclusion `skipped`, and GitHub treats
    `skipped` as a PASS for a required status check — so a gate that skipped on
    the duplicate label runs would let their newest suite override a genuine
    `failure` from the `opened` run, and the required context would become
    decorative on exactly the auto-merge path it guards (FND-2167).
    """
    assert str(_gate_job().get("if")).strip() == "always()"
