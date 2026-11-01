"""Tests for .github/scripts/gate_check_verdict.py (FND-3839).

A `/gate-check` dispatches several workflows and leaves a pending
`gate-check` status listing them; this driver runs on every one of their
completions. What has to hold:

* nothing is decided until every listed workflow has a completed run created
  after the request — an older run on the same commit (an earlier `/gate-check
  force`) cannot answer for this one;
* the verdict is `success` only when all of them succeeded, and a failure names
  each failed workflow;
* a recorded pass is never overwritten, so a late duplicate event cannot
  flip it; a recorded failure gives way to `success` only once a re-run of
  what failed has passed, read from a request the bot posted (FND-4331);
* an unknown request time settles nothing (lens F-3dd6c5, F-387afb);
* once final, the newest Release Gate run is re-run unless its latest attempt
  started after the verdict — idempotent, so every later callback retries a
  failed or overdue re-run (lens F-76a87b) and overlapping callbacks re-run it
  once (lens F-f4188f).
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
import test_command_comment as tcc  # noqa: E402
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

    def test_unknown_request_time_settles_nothing(self) -> None:
        """An earlier `/gate-check force`'s green runs must not settle a new
        request whose own time cannot be read."""
        runs = {"tests.yaml": [_run(1, created=1)], "checks.yml": [_run(2, created=1)]}
        verdict = gcv.resolve(EXPECTED, runs, None)
        assert verdict.final is False
        assert "request time" in verdict.reason

    def test_all_success(self) -> None:
        runs = {"tests.yaml": [_run(1)], "checks.yml": [_run(2)]}
        verdict = gcv.resolve(EXPECTED, runs, REQUESTED)
        assert verdict == gcv.Verdict(
            True,
            "success",
            "passed: tests.yaml checks.yml",
            "https://example.invalid/runs/1",
            rows=(
                ("tests.yaml", "success", "https://example.invalid/runs/1"),
                ("checks.yml", "success", "https://example.invalid/runs/2"),
            ),
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
        written_at: str = _iso(40),
        rerun_fails: bool = False,
        history: list[dict[str, Any]] | None = None,
        arrives_before_write: list[dict[str, Any] | None] | None = None,
    ) -> None:
        self.status = status
        self.status_created_at = status_created_at
        self.status_url = COMMAND_URL
        # Older `gate-check` rows under the current one, newest first.
        self.history = history or []
        # Rows another workflow posts just before each of this driver's status
        # writes lands, in write order (a racing `/gate-check force`).
        self.arrives_before_write = list(arrives_before_write or [])
        self.written_at = written_at
        self.rerun_fails = rerun_fails
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
            if "/statuses/" in path:
                row = (
                    self.arrives_before_write.pop(0)
                    if self.arrives_before_write
                    else None
                )
                if row is not None:
                    self._push(row["state"], row["description"])
                    self.status_created_at = row["created_at"]
                    self.status_url = row["target_url"]
                # Read back like GitHub: the newest row wins.
                self._push(fields["state"], fields["description"])
                self.status_created_at = self.written_at
                self.status_url = fields["target_url"]
            if path.endswith("/rerun") and self.rerun_fails:
                return subprocess.CompletedProcess(args, 1, "", "409 already running")
            return subprocess.CompletedProcess(args, 0, "{}", "")
        path = next(a for a in args[1:] if a.startswith("repos/"))
        self.paths.append(path)
        current = [self._row()] if self.status is not None else []
        if path.endswith("/status"):
            body: Any = {"statuses": current}
        elif "/statuses?" in path:
            rows = current + self.history
            body = [rows] if "--paginate" in args else rows
        elif "/actions/workflows/" in path:
            workflow = path.split("/workflows/")[1].split("/")[0]
            queue = self.runs.get(workflow, [[]])
            listing = queue.pop(0) if len(queue) > 1 else queue[0]
            if "event=" in path:
                event = path.split("event=")[1].split("&")[0]
                listing = [r for r in listing if r.get("event") == event]
            body = (
                [{"workflow_runs": listing}]
                if "--paginate" in args
                else {"workflow_runs": listing}
            )
        else:
            raise AssertionError(f"unexpected gh call: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    def _push(self, state: str, description: str) -> None:
        if self.status is not None:
            self.history.insert(0, self._row())
        self.status = (state, description)

    def _row(self) -> dict[str, Any]:
        assert self.status is not None
        return _status_row(
            *self.status, created=self.status_created_at, url=self.status_url
        )


BOT = "github-actions[bot]"
#: The command run that posted a `/gate-check` request: one per request.
COMMAND_URL = "https://example.invalid/command/1"


def _status_row(
    state: str,
    description: str,
    *,
    created: str,
    creator: str = BOT,
    url: str = COMMAND_URL,
) -> dict[str, Any]:
    return {
        "context": "gate-check",
        "state": state,
        "description": description,
        "target_url": url,
        "created_at": created,
        "creator": {"login": creator},
    }


def _gate(
    run_id: int,
    status: str = "completed",
    *,
    started: int = 5,
    conclusion: str = "failure",
) -> dict[str, Any]:
    """A Release Gate run whose latest attempt started at ``started``."""
    run = _run(
        run_id, status=status, event="pull_request", created=5, conclusion=conclusion
    )
    run["run_started_at"] = _iso(started)
    return run


VERDICT_AT = datetime(2026, 10, 13, 12, 40, tzinfo=timezone.utc)


class TestSettle:
    def test_no_request_is_left_alone(self) -> None:
        api = FakeApi(status=None, runs={"tests.yaml": [[_run(1)]]})
        verdict = gcv.settle(GitHub(REPO, api), SHA)
        assert verdict.final is False
        assert api.writes == []

    @pytest.mark.parametrize(
        "status", [("success", "passed: tests.yaml"), ("failure", "failed: x")]
    )
    def test_a_recorded_verdict_is_never_rewritten(
        self, status: tuple[str, str]
    ) -> None:
        """A late duplicate completion must not flip a recorded verdict; it
        only re-checks the Release Gate (here already up to date). The failure
        has no re-run to read: see TestRerunAfterFailure for one that does."""
        api = FakeApi(
            status=status,
            status_created_at=_iso(40),
            runs={gcv.RELEASE_GATE_WORKFLOW: [[_gate(8, started=41)]]},
        )
        verdict = gcv.settle(GitHub(REPO, api), SHA)
        assert (verdict.final, verdict.state) == (True, status[0])
        assert api.writes == []

    def test_a_failed_rerun_is_retried_by_the_next_callback(self) -> None:
        """lens F-76a87b: the status went final but the Release Gate re-run
        did not happen, so the gate still shows the red it read from
        `pending`. The next callback must re-run it, not exit."""
        api = FakeApi(
            status=("success", "passed: tests.yaml"),
            status_created_at=_iso(40),
            runs={gcv.RELEASE_GATE_WORKFLOW: [[_gate(8, started=35)]]},
        )
        gcv.settle(GitHub(REPO, api), SHA)
        assert [p for p, _ in api.writes] == [f"repos/{REPO}/actions/runs/8/rerun"]

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


REQUEST = _status_row("pending", "running: tests.yaml checks.yml", created=_iso(30))
#: A later `/gate-check force`, made by another command run.
FORCE = _status_row(
    "pending",
    "running: tests.yaml",
    created=_iso(45),
    url="https://example.invalid/command/2",
)
#: A second force after that one, made by a third command run.
FORCE_AGAIN = _status_row(
    "pending",
    "running: checks.yml",
    created=_iso(47),
    url="https://example.invalid/command/3",
)


class TestFindRequest:
    def test_ranks_requests_by_their_first_row_not_the_newest_row(self) -> None:
        rows = [
            _status_row("pending", "running: tests.yaml", created=_iso(50)),
            FORCE_AGAIN,
            REQUEST,
        ]
        request, requested_at = gcv.find_request(rows)
        assert request == FORCE_AGAIN
        assert requested_at == datetime(2026, 10, 13, 12, 47, tzinfo=timezone.utc)

    def test_an_undated_request_settles_nothing(self) -> None:
        undated = dict(REQUEST, created_at=None)
        assert gcv.find_request([undated]) == (undated, None)

    def test_rows_that_are_not_requests_are_skipped(self) -> None:
        rows = [
            _status_row("failure", "failed: x", created=_iso(40)),
            _status_row("pending", "running: x", created=_iso(41), creator="x"),
        ]
        assert gcv.find_request(rows) == (None, None)


class TestRerunAfterFailure:
    """FND-4331: "Re-run failed jobs" on a dispatched run that failed the gate.

    The re-run is a new attempt of the same run, and its completion fires the
    same callback. When it passes, the recorded `failure` must give way to
    `success` and Release Gate must be re-run to read it; otherwise the only
    way out is a full `/gate-check force`.
    """

    def _api(
        self,
        tests_run: dict[str, Any],
        *,
        state: str = "failure",
        request: dict[str, Any] = REQUEST,
    ) -> FakeApi:
        return FakeApi(
            status=(state, "failed: tests.yaml (failure)"),
            status_created_at=_iso(40),
            written_at=_iso(50),
            history=[request],
            runs={
                "tests.yaml": [[tests_run]],
                "checks.yml": [[_run(2)]],
                # Read the `failure` when it was written.
                gcv.RELEASE_GATE_WORKFLOW: [[_gate(8, started=41)]],
            },
        )

    @pytest.mark.parametrize("state", ["failure", "error"])
    def test_a_passing_rerun_turns_the_verdict_green(self, state: str) -> None:
        api = self._api(_run(1), state=state)
        verdict = gcv.settle(GitHub(REPO, api), SHA)
        assert (verdict.final, verdict.state) == (True, "success")
        status_path, fields = api.writes[0]
        assert status_path == f"repos/{REPO}/statuses/{SHA}"
        assert (fields["context"], fields["state"]) == ("gate-check", "success")
        assert fields["description"] == "passed: tests.yaml checks.yml"
        assert [p for p, _ in api.writes[1:]] == [f"repos/{REPO}/actions/runs/8/rerun"]

    def test_the_new_verdict_is_shown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        shown: list[str] = []
        monkeypatch.setattr(
            gcv, "show_verdict", lambda g, s, b, verdict: shown.append(verdict.state)
        )
        gcv.settle(GitHub(REPO, self._api(_run(1))), SHA, branch="bump-version-main")
        assert shown == ["success"]

    @pytest.mark.parametrize(
        "tests_run",
        [
            _run(1, status="in_progress", conclusion=None),
            _run(1, conclusion="failure"),
        ],
        ids=["rerun-still-running", "rerun-failed-again"],
    )
    def test_the_failure_stands_until_the_rerun_passes(
        self, tests_run: dict[str, Any]
    ) -> None:
        api = self._api(tests_run)
        verdict = gcv.settle(GitHub(REPO, api), SHA)
        assert (verdict.final, verdict.state) == (True, "failure")
        assert api.writes == []

    def test_a_request_not_posted_by_the_bot_is_ignored(self) -> None:
        """FND-3947: only github-actions[bot]'s rows are trusted. A `pending`
        anyone with write access posted must not get a bot-signed pass."""
        forged = _status_row(
            "pending", "running: tests.yaml checks.yml", created=_iso(30), creator="x"
        )
        api = self._api(_run(1), request=forged)
        assert gcv.settle(GitHub(REPO, api), SHA).state == "failure"
        assert api.writes == []

    def test_runs_older_than_the_request_still_do_not_count(self) -> None:
        api = self._api(_run(1, created=10))
        assert gcv.settle(GitHub(REPO, api), SHA).state == "failure"
        assert api.writes == []

    def test_a_force_request_racing_the_write_is_put_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lens F-7d5bea: a `/gate-check force` lands between the read and the
        write. The pass must not mask it, or the forced runs' callbacks find a
        recorded pass and never settle them."""
        shown: list[str] = []
        monkeypatch.setattr(
            gcv, "show_verdict", lambda g, s, b, verdict: shown.append(verdict.state)
        )
        api = self._api(_run(1))
        api.arrives_before_write = [FORCE]
        verdict = gcv.settle(GitHub(REPO, api), SHA)
        assert verdict.final is False
        assert [f["state"] for _, f in api.writes[:2]] == ["success", "pending"]
        fields = api.writes[1][1]
        assert (fields["description"], fields["target_url"]) == (
            FORCE["description"],
            FORCE["target_url"],
        )
        assert api.status == ("pending", FORCE["description"])
        assert shown == []

    def test_a_second_force_racing_the_put_back_stays_the_request(self) -> None:
        """lens F-f1a9c4: a second `/gate-check force` lands between the
        re-read and the put-back of the first. The put-back is then the
        newest row, but the second force is the newest request, and only its
        runs may settle the commit."""
        api = self._api(_run(1))
        api.arrives_before_write = [FORCE, FORCE_AGAIN]
        gcv.settle(GitHub(REPO, api), SHA)
        rows = GitHub(REPO, api).status_rows(SHA)
        assert rows[0]["target_url"] == FORCE["target_url"]  # the put-back
        request, requested_at = gcv.find_request(rows)
        assert request is not None
        assert request["target_url"] == FORCE_AGAIN["target_url"]
        assert requested_at == datetime(2026, 10, 13, 12, 47, tzinfo=timezone.utc)
        # The next callback waits on the second force's own runs (checks.yml
        # has none since it was made), never on the first force's passing one.
        api.writes.clear()
        verdict = gcv.settle(GitHub(REPO, api), SHA)
        assert verdict.final is False
        assert verdict.reason == "checks.yml has not started yet"
        assert api.writes == []

    def test_the_put_back_request_keeps_its_own_time(self) -> None:
        """The forced runs were created before the re-post. Dated by the
        re-post, they would never count and the request would never settle."""
        api = FakeApi(
            status=("pending", FORCE["description"]),
            status_created_at=_iso(50),
            history=[
                _status_row(
                    "success", "passed: tests.yaml checks.yml", created=_iso(49)
                ),
                FORCE,
            ],
            runs={
                "tests.yaml": [[_run(3, created=47)]],
                gcv.RELEASE_GATE_WORKFLOW: [[_gate(8, started=51)]],
            },
        )
        api.status_url = FORCE["target_url"]
        verdict = gcv.settle(GitHub(REPO, api), SHA)
        assert (verdict.final, verdict.state) == (True, "success")

    def test_a_recorded_pass_is_never_rewritten(self) -> None:
        """A later re-run that fails does not reach back into a passed gate."""
        api = self._api(_run(1, conclusion="failure"), state="success")
        assert gcv.settle(GitHub(REPO, api), SHA).state == "success"
        assert api.writes == []


class TestSettleShowsTheVerdict:
    def _record(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
        shown: list[tuple[str, str]] = []
        monkeypatch.setattr(
            gcv,
            "show_verdict",
            lambda github, sha, branch, verdict: shown.append((branch, verdict.state)),
        )
        return shown

    def test_a_fresh_verdict_is_shown_on_the_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shown = self._record(monkeypatch)
        api = FakeApi(
            status=("pending", "running: tests.yaml"),
            runs={"tests.yaml": [[_run(1)]], gcv.RELEASE_GATE_WORKFLOW: [[_gate(8)]]},
        )
        gcv.settle(GitHub(REPO, api), SHA, branch="bump-version-main")
        assert shown == [("bump-version-main", "success")]

    def test_a_recorded_verdict_is_not_shown_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every later callback re-checks the gate; the comment is not re-edited."""
        shown = self._record(monkeypatch)
        api = FakeApi(
            status=("success", "passed: tests.yaml"),
            status_created_at=_iso(40),
            runs={gcv.RELEASE_GATE_WORKFLOW: [[_gate(8, started=41)]]},
        )
        gcv.settle(GitHub(REPO, api), SHA, branch="bump-version-main")
        assert shown == []

    def test_main_passes_the_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def fake_settle(github: GitHub, sha: str, *, branch: str = "") -> gcv.Verdict:
            seen.append(branch)
            return gcv.Verdict(False, reason="x")

        monkeypatch.setattr(gcv, "settle", fake_settle)
        gcv.main(["--repo", REPO, "--head-sha", SHA, "--branch", "bump-version-main"])
        assert seen == ["bump-version-main"]


class StickyApi:
    """`gh api` for show_verdict: the open-PR lookup and the comment endpoints."""

    def __init__(
        self,
        *,
        prs: list[dict[str, Any]] | None = None,
        comments: list[dict[str, Any]] | None = None,
        fail: tuple[str, ...] = (),
    ) -> None:
        self.prs = prs if prs is not None else [{"number": 12}]
        self.comments = comments or []
        self.fail = fail
        self.paths: list[str] = []
        self.posted: list[tuple[str, str]] = []
        self.patched: list[tuple[str, str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        joined = " ".join(args)
        if any(needle in joined for needle in self.fail):
            return subprocess.CompletedProcess(args, 1, "", "boom")
        if "-X" in args:
            path = args[args.index("-X") + 2]
            body = next(a for a in args if a.startswith("body=")).split("=", 1)[1]
            target = self.patched if "PATCH" in args else self.posted
            target.append((path, body))
            return subprocess.CompletedProcess(args, 0, "{}", "")
        path = next(a for a in args[1:] if a.startswith("repos/"))
        self.paths.append(path)
        if "/pulls?" in path:
            out: Any = [self.prs]
        elif "/comments?" in path:
            out = [self.comments]
        else:
            raise AssertionError(f"unexpected gh call: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(out), "")


FAILED_VERDICT = gcv.Verdict(
    True,
    "failure",
    "failed: checks.yml (failure)",
    "https://example.invalid/runs/2",
    rows=(
        ("tests.yaml", "success", "https://example.invalid/runs/1"),
        ("checks.yml", "failure", "https://example.invalid/runs/2"),
    ),
)


class TestShowVerdict:
    def test_posts_the_gate_check_sticky_on_the_open_pr(self) -> None:
        api = StickyApi()
        gcv.show_verdict(GitHub(REPO, api), SHA, "bump-version/main", FAILED_VERDICT)
        # The PR is looked up by `owner:branch`, URL-quoted.
        assert any(
            "state=open" in p and "head=atlanhq:bump-version%2Fmain" in p
            for p in api.paths
        )
        ((path, body),) = api.posted
        assert path == f"repos/{REPO}/issues/12/comments"
        assert body.startswith(tcc.marker(tcc.GATE_CHECK))
        assert f"❌ failed on `{SHA[:7]}`" in body
        assert "1 of 2 workflow(s) failed" in body
        assert "[first failure](https://example.invalid/runs/2)" in body
        assert "| `checks.yml` | [❌](https://example.invalid/runs/2) |" in body

    def test_edits_an_existing_sticky(self) -> None:
        mark = tcc.marker(tcc.GATE_CHECK)
        api = StickyApi(
            comments=[
                {"id": 4, "body": f"{mark}\nrequested"},
                {"id": 5, "body": tcc.marker(tcc.E2E)},
            ]
        )
        passed = gcv.Verdict(
            True,
            "success",
            "passed: tests.yaml",
            "https://example.invalid/runs/1",
            rows=(("tests.yaml", "success", "https://example.invalid/runs/1"),),
        )
        gcv.show_verdict(GitHub(REPO, api), SHA, "bump-version-main", passed)
        assert api.posted == []
        ((path, body),) = api.patched
        assert path == f"repos/{REPO}/issues/comments/4"
        assert "✅ passed" in body and "1 workflow(s) passed" in body
        assert "[runs](https://example.invalid/runs/1)" in body

    def test_no_branch_does_nothing(self) -> None:
        api = StickyApi()
        gcv.show_verdict(GitHub(REPO, api), SHA, "", FAILED_VERDICT)
        assert api.paths == [] and api.posted == []

    def test_no_open_pr_does_nothing(self) -> None:
        api = StickyApi(prs=[])
        gcv.show_verdict(GitHub(REPO, api), SHA, "bump-version-main", FAILED_VERDICT)
        assert api.posted == [] and api.patched == []

    @pytest.mark.parametrize("failing", ["/pulls?", "/comments?", "POST"])
    def test_never_raises(self, capsys, failing: str) -> None:
        """The status is already written; the comment is only its view."""
        api = StickyApi(fail=(failing,))
        gcv.show_verdict(GitHub(REPO, api), SHA, "bump-version-main", FAILED_VERDICT)
        assert "::warning::" in capsys.readouterr().err


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
        assert gcv.rerun_release_gate(
            GitHub(REPO, api), SHA, VERDICT_AT, sleep=sleeps.append
        )
        assert sleeps == [20, 20]
        assert [p for p, _ in api.writes] == [f"repos/{REPO}/actions/runs/8/rerun"]

    def test_a_gate_that_read_the_verdict_is_left_alone(self) -> None:
        """Idempotence: the second of two overlapping callbacks finds the
        re-run already started after the verdict and does nothing."""
        api = FakeApi(
            status=None,
            runs={
                gcv.RELEASE_GATE_WORKFLOW: [
                    [_gate(8, started=41, conclusion="success")]
                ]
            },
        )
        assert gcv.rerun_release_gate(GitHub(REPO, api), SHA, VERDICT_AT) is False
        assert api.writes == []

    def test_a_rerun_rejected_because_it_is_already_running_is_fine(self) -> None:
        """lens F-f4188f: two callbacks race to re-run; the loser's 409 is
        the winner's success, not a failure."""
        api = FakeApi(
            status=None,
            rerun_fails=True,
            runs={
                gcv.RELEASE_GATE_WORKFLOW: [
                    [_gate(8)],
                    [_gate(8, "queued", started=41)],
                ]
            },
        )
        assert gcv.rerun_release_gate(GitHub(REPO, api), SHA, VERDICT_AT) is False

    def test_a_rerun_that_really_failed_raises(self) -> None:
        """So the callback fails visibly and the next one retries."""
        api = FakeApi(
            status=None,
            rerun_fails=True,
            runs={gcv.RELEASE_GATE_WORKFLOW: [[_gate(8)]]},
        )
        with pytest.raises(GitHubError):
            gcv.rerun_release_gate(GitHub(REPO, api), SHA, VERDICT_AT)

    def test_raises_after_the_attempts(self) -> None:
        api = FakeApi(
            status=None,
            runs={gcv.RELEASE_GATE_WORKFLOW: [[_gate(8, "in_progress")]]},
        )
        sleeps: list[float] = []
        with pytest.raises(GitHubError, match="Release Gate run 8 did not finish"):
            gcv.rerun_release_gate(
                GitHub(REPO, api), SHA, VERDICT_AT, attempts=3, sleep=sleeps.append
            )
        # No pointless sleep after the last look.
        assert sleeps == [20, 20]
        assert api.writes == []

    def test_no_release_gate_run_is_not_an_error(self) -> None:
        api = FakeApi(
            status=None,
            runs={gcv.RELEASE_GATE_WORKFLOW: [[_run(3, event="workflow_dispatch")]]},
        )
        assert gcv.rerun_release_gate(GitHub(REPO, api), SHA, VERDICT_AT) is False
        assert api.writes == []


class TestMain:
    def test_github_error_fails(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        def boom(github: GitHub, sha: str, **_kwargs: Any) -> gcv.Verdict:
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
        monkeypatch.setattr(gcv, "settle", lambda github, sha, **_kwargs: verdict)
        assert gcv.main(["--repo", REPO, "--head-sha", SHA]) == 0
        assert needle in capsys.readouterr().err
