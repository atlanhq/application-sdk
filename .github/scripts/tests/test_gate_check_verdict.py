"""Tests for .github/scripts/gate_check_verdict.py (FND-3839).

A `/gate-check` dispatches several workflows and leaves a pending
`gate-check` status listing them; this driver runs on every one of their
completions. What has to hold:

* nothing is decided until every listed workflow has a completed run created
  after the request — an older run on the same commit (an earlier `/gate-check
  force`) cannot answer for this one;
* the verdict is `success` only when all of them succeeded, and a failure names
  each failed workflow;
* a status that is no longer `pending` is never overwritten, so a late
  duplicate event cannot flip a recorded verdict;
* once final, the newest Release Gate run is re-run (after waiting out one
  still in flight), so the required check reads the verdict.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import gate_check_verdict as gcv  # noqa: E402
from pr_test_command import GitHub, GitHubError  # noqa: E402

REPO = "atlanhq/atlan-example-app"
SHA = "a96a27b0000000000000000000000000000000aa"
EXPECTED = ["tests.yaml", "checks.yml"]
REQUESTED = datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc)


def _iso(minute: int) -> str:
    return f"2026-10-13T12:{minute:02d}:00Z"


def _run(
    run_id: int,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    created: int = 31,
    event: str = "workflow_dispatch",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "created_at": _iso(created),
        "event": event,
        "html_url": f"https://example.invalid/runs/{run_id}",
    }


# ── resolve ────────────────────────────────────────────────────────────────


class TestResolve:
    def test_empty_expected_is_never_final(self) -> None:
        """A malformed description must not settle as a vacuous pass."""
        verdict = gcv.resolve([], {}, REQUESTED)
        assert verdict.final is False
        assert "lists no workflows" in verdict.reason

    def test_a_workflow_not_started_yet_waits(self) -> None:
        verdict = gcv.resolve(EXPECTED, {"tests.yaml": [_run(1)]}, REQUESTED)
        assert verdict.final is False
        assert verdict.reason == "checks.yml has not started yet"

    def test_a_workflow_still_running_waits(self) -> None:
        runs = {
            "tests.yaml": [_run(1)],
            "checks.yml": [_run(2, status="in_progress", conclusion=None)],
        }
        verdict = gcv.resolve(EXPECTED, runs, REQUESTED)
        assert verdict.final is False
        assert verdict.reason == "checks.yml is still running"

    def test_runs_created_before_the_request_are_ignored(self) -> None:
        """An earlier request's green run cannot answer this one."""
        runs = {
            "tests.yaml": [_run(1)],
            "checks.yml": [_run(2, created=10)],
        }
        verdict = gcv.resolve(EXPECTED, runs, REQUESTED)
        assert verdict.final is False
        assert "checks.yml has not started" in verdict.reason

    def test_newest_run_after_the_request_counts(self) -> None:
        runs = {
            "tests.yaml": [_run(1, conclusion="failure"), _run(5)],
            "checks.yml": [_run(2)],
        }
        verdict = gcv.resolve(EXPECTED, runs, REQUESTED)
        assert (verdict.final, verdict.state) == (True, "success")

    def test_a_run_without_a_timestamp_is_counted(self) -> None:
        run = _run(2)
        run["created_at"] = None
        verdict = gcv.resolve(
            EXPECTED, {"tests.yaml": [_run(1)], "checks.yml": [run]}, REQUESTED
        )
        assert verdict.final is True

    def test_unknown_request_time_counts_every_run(self) -> None:
        runs = {"tests.yaml": [_run(1, created=1)], "checks.yml": [_run(2, created=1)]}
        assert gcv.resolve(EXPECTED, runs, None).state == "success"

    def test_all_success(self) -> None:
        runs = {"tests.yaml": [_run(1)], "checks.yml": [_run(2)]}
        verdict = gcv.resolve(EXPECTED, runs, REQUESTED)
        assert verdict == gcv.Verdict(
            True,
            "success",
            "passed: tests.yaml checks.yml",
            "https://example.invalid/runs/1",
        )

    def test_mixed_failures_name_each_and_link_the_first(self) -> None:
        expected = ["tests.yaml", "checks.yml", "conformance.yaml"]
        runs = {
            "tests.yaml": [_run(1, conclusion="failure")],
            "checks.yml": [_run(2)],
            "conformance.yaml": [_run(3, conclusion="cancelled")],
        }
        verdict = gcv.resolve(expected, runs, REQUESTED)
        assert verdict.final is True
        assert verdict.state == "failure"
        assert verdict.description == (
            "failed: tests.yaml (failure), conformance.yaml (cancelled)"
        )
        assert verdict.target_url == "https://example.invalid/runs/1"

    def test_skipped_is_not_success(self) -> None:
        """A dispatched gate that skipped did not pass."""
        runs = {"tests.yaml": [_run(1, conclusion="skipped")], "checks.yml": [_run(2)]}
        assert gcv.resolve(EXPECTED, runs, REQUESTED).state == "failure"


# ── settle / rerun_release_gate (I/O through the `gh` seam) ───────────────


class FakeApi:
    """Stand-in for `gh api`, routing on the REST path; records writes."""

    def __init__(
        self,
        *,
        status: tuple[str, str] | None,
        status_created_at: str = _iso(30),
        runs: dict[str, list[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self.status = status
        self.status_created_at = status_created_at
        # workflow → successive listings; the last one sticks.
        self.runs = runs or {}
        self.writes: list[tuple[str, dict[str, str]]] = []
        self.paths: list[str] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        if "-X" in args:
            path = args[args.index("-X") + 2]
            fields = dict(
                args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-f"
            )
            self.writes.append((path, fields))
            return subprocess.CompletedProcess(args, 0, "{}", "")
        path = args[1]
        self.paths.append(path)
        rows: list[dict[str, Any]] = []
        if self.status is not None:
            rows = [
                {
                    "context": "gate-check",
                    "state": self.status[0],
                    "description": self.status[1],
                    "created_at": self.status_created_at,
                }
            ]
        if path.endswith("/status"):
            body: Any = {"statuses": rows}
        elif "/statuses?" in path:
            body = rows
        elif "/actions/workflows/" in path:
            workflow = path.split("/workflows/")[1].split("/")[0]
            queue = self.runs.get(workflow, [[]])
            listing = queue.pop(0) if len(queue) > 1 else queue[0]
            if "event=" in path:
                event = path.split("event=")[1].split("&")[0]
                listing = [r for r in listing if r.get("event") == event]
            body = {"workflow_runs": listing}
        else:
            raise AssertionError(f"unexpected gh call: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")


def _gate(run_id: int, status: str = "completed") -> dict[str, Any]:
    return _run(run_id, status=status, event="pull_request", created=5)


class TestSettle:
    @pytest.mark.parametrize(
        "status", [None, ("success", "passed: tests.yaml"), ("failure", "failed: x")]
    )
    def test_non_pending_status_is_left_alone(
        self, status: tuple[str, str] | None
    ) -> None:
        """A late duplicate completion must not flip a recorded verdict."""
        api = FakeApi(status=status, runs={"tests.yaml": [[_run(1)]]})
        verdict = gcv.settle(GitHub(REPO, api), SHA)
        assert verdict.final is False
        assert "not pending" in verdict.reason
        assert api.writes == []

    def test_unsettled_writes_nothing(self) -> None:
        api = FakeApi(
            status=("pending", "running: tests.yaml checks.yml"),
            runs={"tests.yaml": [[_run(1)]]},
        )
        verdict = gcv.settle(GitHub(REPO, api), SHA)
        assert verdict.final is False
        assert api.writes == []

    def test_final_verdict_is_written_then_release_gate_rerun(self) -> None:
        api = FakeApi(
            status=("pending", "running: tests.yaml checks.yml"),
            runs={
                "tests.yaml": [[_run(1)]],
                "checks.yml": [[_run(2, conclusion="failure")]],
                gcv.RELEASE_GATE_WORKFLOW: [[_gate(7), _gate(8)]],
            },
        )
        verdict = gcv.settle(GitHub(REPO, api), SHA)
        assert verdict.state == "failure"
        status_path, fields = api.writes[0]
        assert status_path == f"repos/{REPO}/statuses/{SHA}"
        assert (fields["context"], fields["state"]) == ("gate-check", "failure")
        assert fields["description"] == "failed: checks.yml (failure)"
        assert fields["target_url"] == "https://example.invalid/runs/2"
        # The newest Release Gate run, after the status is in place.
        assert [p for p, _ in api.writes[1:]] == [f"repos/{REPO}/actions/runs/8/rerun"]

    def test_only_dispatched_runs_are_read(self) -> None:
        """The PR's own runs on the commit are not what `/gate-check` started."""
        api = FakeApi(
            status=("pending", "running: tests.yaml"),
            runs={"tests.yaml": [[_run(1, event="pull_request")]]},
        )
        assert gcv.settle(GitHub(REPO, api), SHA).final is False
        assert any("event=workflow_dispatch" in p for p in api.paths)


class TestRerunReleaseGate:
    def test_waits_for_an_in_flight_gate_then_reruns(self) -> None:
        """A gate still running may already have read `pending`."""
        api = FakeApi(
            status=None,
            runs={
                gcv.RELEASE_GATE_WORKFLOW: [
                    [_gate(8, "in_progress")],
                    [_gate(8, "queued")],
                    [_gate(8)],
                ]
            },
        )
        sleeps: list[float] = []
        assert gcv.rerun_release_gate(GitHub(REPO, api), SHA, sleep=sleeps.append)
        assert sleeps == [20, 20]
        assert [p for p, _ in api.writes] == [f"repos/{REPO}/actions/runs/8/rerun"]

    def test_raises_after_the_attempts(self) -> None:
        api = FakeApi(
            status=None,
            runs={gcv.RELEASE_GATE_WORKFLOW: [[_gate(8, "in_progress")]]},
        )
        sleeps: list[float] = []
        with pytest.raises(GitHubError, match="Release Gate run 8 did not finish"):
            gcv.rerun_release_gate(
                GitHub(REPO, api), SHA, attempts=3, sleep=sleeps.append
            )
        # No pointless sleep after the last look.
        assert sleeps == [20, 20]
        assert api.writes == []

    def test_no_release_gate_run_is_not_an_error(self) -> None:
        api = FakeApi(
            status=None,
            runs={gcv.RELEASE_GATE_WORKFLOW: [[_run(3, event="workflow_dispatch")]]},
        )
        assert gcv.rerun_release_gate(GitHub(REPO, api), SHA) is False
        assert api.writes == []


class TestMain:
    def test_github_error_fails(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        def boom(github: GitHub, sha: str) -> gcv.Verdict:
            raise GitHubError("nope")

        monkeypatch.setattr(gcv, "settle", boom)
        assert gcv.main(["--repo", REPO, "--head-sha", SHA]) == 1
        assert "::error::nope" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("verdict", "needle"),
        [
            (gcv.Verdict(True, "success", "passed: tests.yaml"), "gate-check success"),
            (gcv.Verdict(False, reason="tests.yaml is still running"), "not settled"),
        ],
    )
    def test_reports_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, capsys, verdict: gcv.Verdict, needle: str
    ) -> None:
        monkeypatch.setattr(gcv, "settle", lambda github, sha: verdict)
        assert gcv.main(["--repo", REPO, "--head-sha", SHA]) == 0
        assert needle in capsys.readouterr().err
