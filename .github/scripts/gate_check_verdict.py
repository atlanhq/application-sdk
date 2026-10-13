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
cannot answer for this one. A status that is not `pending` is left alone, so a
late duplicate event cannot flip a recorded verdict.

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
from datetime import datetime
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


def resolve(expected: list, runs: dict, requested_at: datetime | None) -> Verdict:
    """Decide the verdict. Pure.

    ``runs`` maps a workflow file → its `workflow_dispatch` runs on the commit.
    """
    if not expected:
        return Verdict(False, reason="the pending status lists no workflows")
    failed = []
    first_url = ""
    for workflow in expected:
        candidates = [
            r
            for r in runs.get(workflow, [])
            if requested_at is None
            or (_ts(r.get("created_at")) or requested_at) >= requested_at
        ]
        if not candidates:
            return Verdict(False, reason=f"{workflow} has not started yet")
        newest = max(candidates, key=lambda r: r["id"])
        if newest.get("status") != "completed":
            return Verdict(False, reason=f"{workflow} is still running")
        first_url = first_url or newest.get("html_url", "")
        if newest.get("conclusion") != "success":
            failed.append(
                (workflow, newest.get("conclusion"), newest.get("html_url", ""))
            )
    if failed:
        names = ", ".join(f"{w} ({c})" for w, c, _ in failed)
        return Verdict(True, "failure", f"failed: {names}", failed[0][2])
    return Verdict(True, "success", f"passed: {' '.join(expected)}", first_url)


def settle(github: GitHub, sha: str) -> Verdict:
    statuses = github.statuses(sha)
    state, description = statuses.get(GATE_CHECK_CONTEXT, ("", ""))
    if state != "pending":
        return Verdict(
            False, reason=f"`gate-check` is {state or 'absent'}, not pending"
        )
    expected = parse_gate_check_runs(description)
    requested_at = github.status_created_at(sha, GATE_CHECK_CONTEXT)
    runs = {
        workflow: github.runs(sha, event="workflow_dispatch", workflow=workflow)
        for workflow in expected
    }
    verdict = resolve(expected, runs, requested_at)
    if not verdict.final:
        return verdict
    github.status(
        sha, GATE_CHECK_CONTEXT, verdict.state, verdict.description, verdict.target_url
    )
    rerun_release_gate(github, sha)
    return verdict


def rerun_release_gate(
    github: GitHub,
    sha: str,
    *,
    attempts: int = 7,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Re-run the newest Release Gate run on ``sha`` once it has completed.

    A Release Gate still running when the verdict lands may already have read
    `pending`, so it is waited for (it takes seconds) rather than trusted.
    """
    newest: dict = {}
    for attempt in range(max(attempts, 1)):
        runs = [
            r
            for r in github.runs(sha, workflow=RELEASE_GATE_WORKFLOW)
            if r.get("event") == "pull_request"
        ]
        if not runs:
            _notice("no Release Gate run on this commit; nothing to re-run")
            return False
        newest = max(runs, key=lambda r: r["id"])
        if newest.get("status") == "completed":
            github.rerun(int(newest["id"]))
            _notice(f"re-ran Release Gate run {newest['id']}")
            return True
        if attempt < attempts - 1:
            sleep(20)
    raise GitHubError(
        f"Release Gate run {newest.get('id')} did not finish; re-run it by hand to "
        "pick up the gate-check verdict"
    )


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        verdict = settle(GitHub(args.repo), args.head_sha)
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
