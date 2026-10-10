"""Tests for .github/scripts/rerun_deferred_gate.py and its wiring (FND-3650).

``gh`` is stubbed through the shared ``run`` seam, so every decision (re-run,
leave alone, fail open) is exercised with no network access.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import rerun_deferred_gate  # noqa: E402
from rerun_deferred_gate import DEFER_STEP_NAME, main, repair  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate  # noqa: E402

REPO = "atlanhq/atlan-example-app"
SHA = "a96a27b0000000000000000000000000000000aa"
WORKFLOW_ID = 4242
SELF = 38060382427
NEWER = 38060546809

_REUSABLE = (
    Path(__file__).resolve().parents[3] / ".github/workflows/tests-reusable.yaml"
)


def _run(
    run_id: int,
    *,
    conclusion: str = "failure",
    status: str = "completed",
    attempt: int = 1,
) -> dict:
    return {
        "id": run_id,
        "workflow_id": WORKFLOW_ID,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
    }


def _gate_job(
    step_conclusion: str = "failure", step_name: str = DEFER_STEP_NAME
) -> dict:
    return {
        "name": "tests / Tests Gate",
        "steps": [
            {"name": "Enforce gate", "conclusion": "skipped"},
            {"name": step_name, "conclusion": step_conclusion},
        ],
    }


def _stub(
    *,
    listing: list,
    jobs: list | None = None,
    reruns: list | None = None,
    rerun_ok: bool = True,
):
    calls = reruns if reruns is not None else []

    def run(args: list) -> str:
        path = args[-1] if args[1] == "-X" else args[1]
        if path.endswith("/rerun-failed-jobs"):
            calls.append(path)
            return "{}" if rerun_ok else ""
        if path.endswith("/jobs?per_page=100"):
            return "" if jobs is None else json.dumps({"jobs": jobs})
        if path.endswith(f"/actions/runs/{SELF}"):
            return json.dumps({"workflow_id": WORKFLOW_ID})
        return json.dumps([{"workflow_runs": listing}])

    return run


def test_reruns_a_newer_gate_that_gave_up_waiting() -> None:
    reruns: list = []
    run = _stub(
        listing=[_run(SELF, conclusion="success"), _run(NEWER)],
        jobs=[_gate_job()],
        reruns=reruns,
    )
    assert repair(REPO, SHA, SELF, run=run)
    assert reruns == [f"repos/{REPO}/actions/runs/{NEWER}/rerun-failed-jobs"]


@pytest.mark.parametrize(
    ("newest", "jobs"),
    [
        # Still running: it reads the verdict itself.
        (_run(NEWER, status="in_progress", conclusion=None), [_gate_job()]),
        # Passed or was evicted: not this repair's case.
        (_run(NEWER, conclusion="success"), [_gate_job()]),
        (_run(NEWER, conclusion="cancelled"), [_gate_job()]),
        # Already re-run once.
        (_run(NEWER, attempt=2), [_gate_job()]),
        # Failed for another reason (unit, integration).
        (_run(NEWER), [_gate_job(step_conclusion="skipped")]),
        (_run(NEWER), [_gate_job(step_name="Enforce gate")]),
        # Jobs unreadable.
        (_run(NEWER), None),
    ],
)
def test_leaves_the_newest_run_alone(newest: dict, jobs: list | None) -> None:
    reruns: list = []
    assert not repair(
        REPO, SHA, SELF, run=_stub(listing=[newest], jobs=jobs, reruns=reruns)
    )
    assert reruns == []


def test_only_the_newest_run_is_considered() -> None:
    """An older failed gate does not own the required context."""
    reruns: list = []
    listing = [_run(NEWER), _run(NEWER + 1, conclusion="success")]
    assert not repair(
        REPO, SHA, SELF, run=_stub(listing=listing, jobs=[_gate_job()], reruns=reruns)
    )
    assert reruns == []


def test_runs_older_than_this_one_are_ignored() -> None:
    reruns: list = []
    assert not repair(
        REPO,
        SHA,
        SELF,
        run=_stub(listing=[_run(SELF - 1)], jobs=[_gate_job()], reruns=reruns),
    )
    assert reruns == []


def test_a_rejected_rerun_fails_open(capsys: pytest.CaptureFixture[str]) -> None:
    run = _stub(listing=[_run(NEWER)], jobs=[_gate_job()], rerun_ok=False)
    assert not repair(REPO, SHA, SELF, run=run)
    assert "actions: write" in capsys.readouterr().err


def test_main_always_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rerun_deferred_gate, "_run_gh", lambda args: "")
    assert main(["--repo", REPO, "--head-sha", SHA, "--self-run-id", str(SELF)]) == 0


# --- wiring ------------------------------------------------------------------


@pytest.fixture(scope="module")
def gate_job() -> dict[str, Any]:
    return yaml.safe_load(_REUSABLE.read_text(encoding="utf-8"))["jobs"]["tests-passed"]


_REPAIR = "Re-run a newer gate that gave up waiting for this verdict"


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in job["steps"] if s.get("name") == name)


def test_the_step_this_repair_looks_for_exists(gate_job: dict[str, Any]) -> None:
    """A rename in the workflow would silently disable the repair."""
    assert DEFER_STEP_NAME in [s.get("name") for s in gate_job["steps"]]


@pytest.mark.parametrize(
    ("discover", "expected"),
    [("success", True), ("failure", True), ("cancelled", True), ("skipped", False)],
)
def test_only_the_acting_run_repairs(
    gate_job: dict[str, Any], discover: str, expected: bool
) -> None:
    contexts = {
        "github": {"event_name": "pull_request"},
        "needs": {"discover-e2e": {"result": discover}},
    }
    assert evaluate(_step(gate_job, _REPAIR)["if"], contexts) is expected


def test_the_repair_follows_the_recorded_verdict_and_fails_open(
    gate_job: dict[str, Any],
) -> None:
    names = [s.get("name") for s in gate_job["steps"]]
    assert names.index("Record the e2e verdict on the head commit") < names.index(
        _REPAIR
    )
    assert names.index(_REPAIR) < names.index("Enforce gate")
    step = _step(gate_job, _REPAIR)
    assert step["continue-on-error"] is True
    assert step["env"]["GH_TOKEN"] == "${{ secrets.ORG_PAT_GITHUB || github.token }}"
    assert "rerun_deferred_gate.py" in step["run"]
