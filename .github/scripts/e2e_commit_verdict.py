#!/usr/bin/env python3
"""Hold a Tests Gate that did not run e2e to the e2e verdict on its commit.

## The failure this closes (FND-3650)

A PR gets several ``pull_request`` events on one head SHA, and every one of
them creates a ``Tests`` run. Only the run started by adding the ``e2e`` label
runs the live-tenant suite. The others, started by any other label (the review
bot's labels, Renovate's, size/area), skip e2e on purpose so that an unrelated
label does not lease three tenants (FND-48).

A skipped e2e reads as "not requested" to ``verify-test-gate``, so each of those
runs publishes a green ``tests / Tests Gate``. GitHub resolves a required check
to the check run of the NEWEST check suite on the commit, not the last one to
finish (FND-2167). So a label added after ``e2e`` greened the required check
while the suite was still running, and a PR merged before its e2e finished. If
the suite had failed, the same label event would have merged a failed e2e.

## The rule

The run that acts on the label owns the e2e verdict for its commit and records
it as the ``e2e`` commit status: ``pending`` as soon as it starts, then
``success``, ``failure`` or ``error`` (no verdict: cancelled, tenant busy,
install failed). Every other run on that commit defers to the status instead of
reading its own skipped e2e as a pass:

* ``success``: pass.
* ``failure`` / ``error``: fail. This holds after the label is gone, because the
  acting run consumes it (FND-3411) but the status stays on the commit.
* ``pending``: wait for it to resolve, so the gate stays pending rather than
  deciding early. If it outlasts the wait budget, fail with guidance. The acting
  run re-runs this gate when it records its verdict (``rerun_deferred_gate.py``).
* no status: pass. No e2e ran on this commit, and e2e is not required on a
  normal PR. The one exception is a run that WOULD have acted on the label but
  for the FND-48 filter. Its acting sibling may not have posted ``pending`` yet,
  so it waits a short settle window for the status to appear, then fails.

A status that cannot be read is retried until the budget runs out, then fails.
This driver decides a required check, so unlike the repair scripts it fails
closed.

It emits ``passed`` and ``e2e-status`` on stdout for ``$GITHUB_OUTPUT`` and
annotations on stderr. It always exits 0; the gate job enforces ``passed`` in a
branch-free step (docs/standards/ci.md).

Unit-tested in tests/test_e2e_commit_verdict.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Callable

RunFn = Callable[[list], str]
ClockFn = Callable[[], float]
SleepFn = Callable[[float], None]

#: The commit-status context the acting run writes and the Release Gate reads.
E2E_CONTEXT = "e2e"

_SUCCESS = "success"
_PENDING = "pending"
_FAILED = ("failure", "error")

#: How long to wait for a running e2e to resolve. The gate job runs on
#: ``ubuntu-slim``, which GitHub kills at 15 minutes (FND-3335), so this cannot
#: cover a full suite plus its tenant lease. It covers an e2e that is nearly
#: done; a longer one is handed to the acting run's re-run repair
#: (``rerun_deferred_gate.py``), which re-runs this gate once the verdict exists.
_WAIT_BUDGET_SECONDS = 10 * 60

#: How long a run that would have acted on the label waits for its acting
#: sibling to post ``pending``. That sibling posts it as the first step of its
#: first e2e job, so seconds normally suffice. The margin covers a runner queue.
_SETTLE_BUDGET_SECONDS = 4 * 60

_POLL_INTERVAL_SECONDS = 20


def _run_gh(args: list) -> str:
    """Run ``gh`` and return stdout, or "" on any failure (the test seam)."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        if result.stderr:
            print(
                f"::warning::gh {' '.join(args[:2])} failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
        return ""
    return result.stdout


class StatusUnreadable(Exception):
    """The commit's statuses could not be read or parsed."""


def read_e2e_state(repo: str, sha: str, run: RunFn) -> str:
    """The newest ``e2e`` status state on ``sha``, or "" when there is none.

    The combined-status endpoint already reduces each context to its newest
    status, which is the same read the Release Gate makes.

    Raises:
        StatusUnreadable: the API call failed or returned an unexpected shape.
            Never collapsed into "", because "" means "no e2e ran here" and
            passes the gate.
    """
    raw = run(["api", f"repos/{repo}/commits/{sha}/status?per_page=100"])
    if not raw.strip():
        raise StatusUnreadable(f"could not read the statuses on {sha[:7]}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StatusUnreadable(f"the statuses on {sha[:7]} are not valid JSON") from exc
    statuses = payload.get("statuses") if isinstance(payload, dict) else None
    if not isinstance(statuses, list):
        raise StatusUnreadable(f"the statuses on {sha[:7]} have no `statuses` list")
    for status in statuses:
        if isinstance(status, dict) and status.get("context") == E2E_CONTEXT:
            state = status.get("state")
            return state if isinstance(state, str) else ""
    return ""


def decide(
    repo: str,
    sha: str,
    *,
    awaiting_label_run: bool,
    run: RunFn | None = None,
    clock: ClockFn = time.monotonic,
    sleep: SleepFn = time.sleep,
    wait_budget: int = _WAIT_BUDGET_SECONDS,
    settle_budget: int = _SETTLE_BUDGET_SECONDS,
    poll_interval: int = _POLL_INTERVAL_SECONDS,
) -> tuple[bool, str, str]:
    """Return (passed, summary-row text, reason) for this commit's e2e verdict.

    ``awaiting_label_run`` is True when the PR carries ``e2e`` and this run
    would have acted on it if the event had not been an unrelated label. Only
    then is a missing status a reason to wait and, eventually, to fail.

    ``run`` is resolved at call time so stubbing the module seam takes effect.
    """
    run = run or _run_gh
    start = clock()
    while True:
        elapsed = clock() - start
        try:
            state = read_e2e_state(repo, sha, run)
        except StatusUnreadable as exc:
            if elapsed >= wait_budget:
                return (
                    False,
                    "❌ e2e verdict unreadable",
                    f"{exc}, and this gate will not pass without it. Re-run this "
                    "job.",
                )
            sleep(poll_interval)
            continue

        if state == _SUCCESS:
            return (
                True,
                "✅ Passed (e2e run on this commit)",
                ("the e2e run on this commit passed"),
            )
        if state in _FAILED:
            return (
                False,
                "❌ Failed (e2e run on this commit)",
                f"the e2e run on this commit reported `{state}`. A later run on "
                "the same commit cannot override that. Fix and push, or add the "
                "`e2e` label again to retry.",
            )
        if state == _PENDING:
            if elapsed >= wait_budget:
                return (
                    False,
                    "⏳ e2e still running on this commit",
                    f"the e2e run on this commit was still running after "
                    f"{wait_budget // 60} min. This gate is re-run when that run "
                    "records its verdict; re-run it by hand if it is not.",
                )
            sleep(poll_interval)
            continue

        if not awaiting_label_run:
            return (
                True,
                "⊘ Skipped — add `e2e` label to trigger",
                ("no e2e ran on this commit, and e2e is not required on this PR"),
            )
        if elapsed >= settle_budget:
            return (
                False,
                "❌ e2e requested but never reported",
                "the PR carries the `e2e` label, but no e2e run has reported on "
                f"this commit after {settle_budget // 60} min. Remove and add the "
                "`e2e` label again to start one.",
            )
        sleep(poll_interval)


def _flag(value: str) -> bool:
    return value.strip().lower() == "true"


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hold this run's Tests Gate to the e2e verdict on its commit."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument(
        "--head-sha",
        required=True,
        help="The PR head commit (github.event.pull_request.head.sha).",
    )
    parser.add_argument(
        "--awaiting-label-run",
        default="false",
        help='"true" when the PR carries `e2e` and this run would have acted on '
        "it but for the unrelated-label filter (FND-48).",
    )
    parser.add_argument("--wait-budget-seconds", type=int, default=_WAIT_BUDGET_SECONDS)
    parser.add_argument(
        "--settle-budget-seconds", type=int, default=_SETTLE_BUDGET_SECONDS
    )
    parser.add_argument(
        "--poll-interval-seconds", type=int, default=_POLL_INTERVAL_SECONDS
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    passed, row, reason = decide(
        args.repo,
        args.head_sha,
        awaiting_label_run=_flag(args.awaiting_label_run),
        wait_budget=args.wait_budget_seconds,
        settle_budget=args.settle_budget_seconds,
        poll_interval=args.poll_interval_seconds,
    )
    level = "notice" if passed else "error"
    print(f"::{level}::{reason}", file=sys.stderr)
    print(f"passed={'true' if passed else 'false'}")
    print(f"e2e-status={row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
