#!/usr/bin/env python3
"""Resolve a `/gate-check` request into one verdict on its commit (FND-3839).

`/gate-check` (pr_test_command.py) dispatches a bump-version PR's gated
workflows on the head commit and posts a `pending` `gate-check` status whose
description lists them. Each of those runs finishing fires a `workflow_run`
event, and this driver runs on every one of them:

* while any listed workflow has no dispatched run on the commit yet, or one
  still running, it does nothing — a later completion will call it again;
* once every listed workflow's newest dispatched run on the commit has
  completed, it sets `gate-check` to `success` only if all of them succeeded,
  else `failure`, naming the failed ones;
* it then re-runs the PR's newest Release Gate run on the commit, so the
  required "Release Gate" check reads the verdict instead of the `pending` it
  saw before. A re-run is a newer attempt in the same check suite, so its
  result replaces the earlier red.

Only runs on the status's own commit count, and only runs created after the
request: an older run on the same commit (an earlier `/gate-check force`)
cannot answer for this one. When the request time cannot be read, nothing
settles — older runs never stand in for it. A recorded pass is never
re-decided, so a late duplicate event cannot flip it.

A recorded failure is re-checked by every later callback (FND-4331). "Re-run
failed jobs" on a dispatched run makes a new attempt of the same run, whose
completion fires the same event; once every listed workflow's newest run has
passed, the failure gives way to `success` and Release Gate is re-run, so one
re-run leg clears the gate without a full `/gate-check force`. The request is
read from the status history, and only a `pending` row github-actions[bot]
posted counts — the identity Release Gate trusts (FND-3947).

The Release Gate re-run is idempotent and retried by every later callback:
a Release Gate whose latest attempt started after the verdict was written has
read it, and is left alone; any other is re-run. So a re-run that failed, or a
Release Gate still running past the wait, is repaired by the next completion
instead of staying red. Callbacks for one commit are serialised by the
caller's `concurrency:` group (test-commands.yaml); two that overlap anyway
both see the same re-run and the second stands down.

Re-runs and status writes need `actions: write` and `statuses: write`, which the
calling workflow grants its GITHUB_TOKEN. The workflow runs from the default
branch on `workflow_run` and never checks out PR code.

Tested in tests/test_gate_check_verdict.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pr_test_command import (  # noqa: E402
    GATE_CHECK_CONTEXT,
    GitHub,
    GitHubError,
    _notice,
    _ts,
    parse_gate_check_runs,
)

RELEASE_GATE_WORKFLOW = "release-gate.yaml"


@dataclass(frozen=True)
class Verdict:
    final: bool
    state: str = ""
    description: str = ""
    target_url: str = ""
    reason: str = ""
    #: (workflow, conclusion, url) per gated workflow, for the PR comment.
    rows: tuple = ()


def resolve(expected: list, runs: dict, requested_at: datetime | None) -> Verdict:
    """Decide the verdict. Pure.

    ``runs`` maps a workflow file → its `workflow_dispatch` runs on the commit.
    """
    if not expected:
        return Verdict(False, reason="the pending status lists no workflows")
    if requested_at is None:
        # Without the request time, a run from an earlier request on this
        # commit is indistinguishable from this request's own (lens F-3dd6c5).
        return Verdict(False, reason="the request time could not be read")
    failed = []
    rows = []
    first_url = ""
    for workflow in expected:
        candidates = [
            r
            for r in runs.get(workflow, [])
            if (_ts(r.get("created_at")) or requested_at) >= requested_at
        ]
        if not candidates:
            return Verdict(False, reason=f"{workflow} has not started yet")
        newest = max(candidates, key=lambda r: r["id"])
        if newest.get("status") != "completed":
            return Verdict(False, reason=f"{workflow} is still running")
        first_url = first_url or newest.get("html_url", "")
        rows.append((workflow, newest.get("conclusion"), newest.get("html_url", "")))
        if newest.get("conclusion") != "success":
            failed.append(
                (workflow, newest.get("conclusion"), newest.get("html_url", ""))
            )
    if failed:
        names = ", ".join(f"{w} ({c})" for w, c, _ in failed)
        return Verdict(
            True, "failure", f"failed: {names}", failed[0][2], rows=tuple(rows)
        )
    return Verdict(
        True, "success", f"passed: {' '.join(expected)}", first_url, rows=tuple(rows)
    )


def settle(
    github: GitHub,
    sha: str,
    *,
    branch: str = "",
    sleep: Callable[[float], None] = time.sleep,
) -> Verdict:
    statuses = github.statuses(sha)
    state, description = statuses.get(GATE_CHECK_CONTEXT, ("", ""))
    if state == "pending":
        verdict = _resolve_request(github, sha, *find_request(github.status_rows(sha)))
        if not verdict.final:
            return verdict
        github.status(
            sha,
            GATE_CHECK_CONTEXT,
            verdict.state,
            verdict.description,
            verdict.target_url,
        )
        show_verdict(github, sha, branch, verdict)
    elif state in FINAL_STATES:
        # Already recorded — possibly by a callback whose Release Gate re-run
        # failed. Re-check the Release Gate rather than exiting (F-76a87b).
        verdict = Verdict(True, state, description, reason="already recorded")
        if state != "success":
            verdict = recheck_failure(github, sha, branch) or verdict
    else:
        return Verdict(False, reason="no `gate-check` request on this commit")
    verdict_at = github.status_created_at(sha, GATE_CHECK_CONTEXT)
    rerun_release_gate(github, sha, verdict_at, sleep=sleep)
    return verdict


FINAL_STATES = ("success", "failure", "error")

#: The identity the request and the verdict are posted as; the only one
#: Release Gate trusts (FND-3947).
VERDICT_CREATOR = "github-actions[bot]"


_UNDATED = datetime.min.replace(tzinfo=timezone.utc)


def _is_request(row: dict) -> bool:
    return (
        row.get("context") == GATE_CHECK_CONTEXT
        and row.get("state") == "pending"
        and (row.get("creator") or {}).get("login") == VERDICT_CREATOR
    )


def find_request(rows: list) -> tuple[dict | None, datetime | None]:
    """The newest `/gate-check` request in ``rows`` and the time it was made.

    ``rows`` is the commit's status history, newest first. Only a `pending`
    `gate-check` row ``VERDICT_CREATOR`` posted is a request: anyone with write
    access can post a status, and theirs must never be settled into a
    bot-signed pass.

    A request is identified by its link, the command run that made it, and
    dated by its FIRST row. ``recheck_failure`` re-posts a request it masked,
    so the newest row need not be the newest request: ranking by the first
    row means a re-post can neither move a request's time past the runs it
    dispatched nor put an older request above a newer one (lens F-f1a9c4).
    Returns the request's newest row (its description) and its time; an
    undated request ranks last and settles nothing.
    """
    groups: dict[str, list] = {}
    for index, row in enumerate(rows):
        if _is_request(row):
            groups.setdefault(row.get("target_url") or f"#{index}", []).append(row)
    if not groups:
        return None, None

    def first_at(group: list) -> datetime | None:
        times = [_ts(r.get("created_at")) for r in group]
        return None if None in times else min(times)

    newest = max(groups.values(), key=lambda group: first_at(group) or _UNDATED)
    return newest[0], first_at(newest)


def recheck_failure(github: GitHub, sha: str, branch: str) -> Verdict | None:
    """Turn a recorded failure green once a re-run of what failed passes.

    "Re-run failed jobs" makes a new attempt of the same run, so ``resolve``
    reads its latest attempt (FND-4331). Only a pass is written, and a
    recorded pass is never re-decided. Returns the new verdict, or None.
    """
    request, requested_at = find_request(github.status_rows(sha))
    verdict = _resolve_request(github, sha, request, requested_at)
    if request is None or not (verdict.final and verdict.state == "success"):
        return None
    github.status(
        sha, GATE_CHECK_CONTEXT, verdict.state, verdict.description, verdict.target_url
    )
    # A `/gate-check force` (own concurrency group) may have landed since the
    # read, and this pass masks it (lens F-7d5bea): re-read, and put the newest
    # request back on top. That write is unconditional too, so a request made
    # after this re-read can land UNDER the re-post. It still wins: find_request
    # ranks requests by their FIRST row, and the re-post is only a later row of
    # an older request, never a newer request (lens F-f1a9c4).
    newer, _ = find_request(github.status_rows(sha))
    if newer is not None and newer.get("target_url") != request.get("target_url"):
        github.status(
            sha,
            GATE_CHECK_CONTEXT,
            "pending",
            newer.get("description") or "",
            newer.get("target_url") or "",
        )
        return Verdict(False, reason="a newer `/gate-check` request is running")
    show_verdict(github, sha, branch, verdict)
    return verdict


def _resolve_request(
    github: GitHub, sha: str, request: dict | None, requested_at: datetime | None
) -> Verdict:
    """``resolve`` the request's listed workflows against their dispatched runs."""
    if request is None:
        return Verdict(False, reason="no `/gate-check` request on this commit")
    expected = parse_gate_check_runs(request.get("description") or "")
    runs = {
        workflow: github.runs(sha, event="workflow_dispatch", workflow=workflow)
        for workflow in expected
    }
    return resolve(expected, runs, requested_at)


def show_verdict(github: GitHub, sha: str, branch: str, verdict: Verdict) -> None:
    """Edit the PR's sticky `/gate-check` comment with the verdict. A view of
    the status just written, so it is best effort and never raises."""
    if not branch:
        return
    import test_command_comment as sticky  # noqa: PLC0415

    try:
        pr = github.find_open_pr(branch)
    except GitHubError as error:
        print(
            f"::warning::could not find the PR for {branch}: {error}", file=sys.stderr
        )
        return
    if not pr:
        return
    passed = verdict.state == "success"
    failed = sum(1 for _, conclusion, _ in verdict.rows if conclusion != "success")
    link = sticky.Link("runs" if passed else "first failure", verdict.target_url)
    sticky.try_upsert(
        github,
        pr,
        sticky.CommentState(
            command=sticky.GATE_CHECK,
            stage=sticky.PASSED if passed else sticky.FAILED,
            sha=sha,
            summary=(
                f"{len(verdict.rows)} workflow(s) passed"
                if passed
                else f"{failed} of {len(verdict.rows)} workflow(s) failed"
            ),
            links=(link,) if verdict.target_url else (),
            details_title="Results",
            details=sticky.gate_check_table(list(verdict.rows)),
            note="Release Gate is re-run to read this verdict.",
        ),
    )


def _read_verdict(run: dict, verdict_at: datetime | None) -> bool:
    """Whether this Release Gate run's latest attempt started after the
    verdict was written, i.e. read it."""
    started = _ts(run.get("run_started_at"))
    return verdict_at is not None and started is not None and started >= verdict_at


def _newest_release_gate(github: GitHub, sha: str) -> dict | None:
    runs = [
        r
        for r in github.runs(sha, workflow=RELEASE_GATE_WORKFLOW)
        if r.get("event") == "pull_request"
    ]
    return max(runs, key=lambda r: r["id"]) if runs else None


def rerun_release_gate(
    github: GitHub,
    sha: str,
    verdict_at: datetime | None,
    *,
    attempts: int = 7,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Bring the newest Release Gate run on ``sha`` up to the verdict.

    Idempotent: a run whose latest attempt started after the verdict has read
    it and is left alone, so every callback may call this. A run still in
    progress is waited for (it takes seconds): it may have read `pending`.
    Returns whether it re-ran anything; raises when the run cannot be brought
    up to date, so the callback fails visibly and the next one retries.
    """
    newest: dict | None = None
    for attempt in range(max(attempts, 1)):
        newest = _newest_release_gate(github, sha)
        if newest is None:
            _notice("no Release Gate run on this commit; nothing to re-run")
            return False
        if _read_verdict(newest, verdict_at):
            if newest.get("status") == "completed":
                _notice(f"Release Gate run {newest['id']} already read the verdict")
                return False
        elif newest.get("status") == "completed":
            try:
                github.rerun(int(newest["id"]))
            except GitHubError:
                # A concurrent callback may have re-run it a moment ago; that
                # is success too. Anything else is a real failure.
                again = _newest_release_gate(github, sha)
                if again is not None and (
                    again.get("status") != "completed"
                    or _read_verdict(again, verdict_at)
                ):
                    _notice(f"Release Gate run {newest['id']} is already re-running")
                    return False
                raise
            _notice(f"re-ran Release Gate run {newest['id']}")
            return True
        if attempt < attempts - 1:
            sleep(20)
    raise GitHubError(
        f"Release Gate run {(newest or {}).get('id')} did not finish; the next "
        "gate-check callback retries, or re-run it by hand"
    )


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument(
        "--branch", default="", help="workflow_run.head_branch, to find the PR"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        verdict = settle(GitHub(args.repo), args.head_sha, branch=args.branch)
    except GitHubError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    _notice(
        f"gate-check {verdict.state}: {verdict.description}"
        if verdict.final
        else f"gate-check not settled yet: {verdict.reason}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
