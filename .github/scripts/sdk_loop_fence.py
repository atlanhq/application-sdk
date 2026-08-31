#!/usr/bin/env python3
"""Decide whether an `@sdk-loop` run may start, and pin its baseline.

Three jobs, in order, because each is cheaper than the next:

1. **Authorize.** Same bar as the existing lanes: OWNER / MEMBER / COLLABORATOR.
2. **Dismiss a duplicate.** If a loop is already running on this PR, the new
   one exits successfully having said so. It is NOT queued — by the time a
   queued run started, the live loop would have advanced the branch and the
   queued run would be answering a stale request. First run wins.
3. **Pin the baseline.** Record the head sha every later phase fences against.

Environment:
    GH_TOKEN            App installation token
    REPO                owner/name
    PR_NUMBER           the PR to drive
    COMMENT_ID          the triggering comment (for ANSWERS_TRIGGER)
    AUTHOR_ASSOCIATION  from the comment payload
    RUN_ID              this workflow run, so it can exclude itself
    WORKFLOW_FILE       e.g. sdk-loop.yml
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable

from sdk_loop_common import emit_outputs

AUTHORIZED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: Run statuses that mean "still going". `requested` and `waiting` are
#: pre-start states — a run sitting in either has not touched the branch yet
#: but WILL, so it counts as live for duplicate purposes.
LIVE_STATUSES = frozenset({"queued", "in_progress", "requested", "waiting", "pending"})


@dataclass(frozen=True)
class FenceDecision:
    proceed: bool
    reason: str
    #: Set when we are standing down in favour of a run already going.
    live_run_id: str = ""


def is_authorized(association: str | None) -> bool:
    return (association or "").upper() in AUTHORIZED_ASSOCIATIONS


def find_live_run(runs: list[dict[str, Any]], pr_number: str, self_run_id: str) -> str:
    """Return the id of another live loop run on this PR, or "".

    A run is matched to a PR through its `pull_requests` list when GitHub
    populates it, and otherwise through the display title, which the workflow
    stamps with the PR number. Relying on only the former silently misses runs
    on forks, where `pull_requests` comes back empty.
    """
    # Bounded so #42 does not match a run on #420. GitHub renders the number
    # bare in the title, so the boundary is "not another digit".
    needle = re.compile(rf"#{re.escape(str(pr_number))}(?!\d)")
    for run in runs:
        run_id = str(run.get("databaseId") or run.get("id") or "")
        if not run_id or run_id == str(self_run_id):
            continue
        if (run.get("status") or "") not in LIVE_STATUSES:
            continue
        prs = run.get("pull_requests") or run.get("pullRequests") or []
        if any(str(p.get("number")) == str(pr_number) for p in prs):
            return run_id
        title = run.get("display_title") or run.get("displayTitle") or ""
        if needle.search(title):
            return run_id
    return ""


def decide(
    association: str | None,
    runs: list[dict[str, Any]],
    pr_number: str,
    self_run_id: str,
) -> FenceDecision:
    if not is_authorized(association):
        return FenceDecision(
            proceed=False,
            reason=(
                f"`@sdk-loop` is restricted to repository collaborators; this "
                f"comment came from `{association or 'an unknown association'}`."
            ),
        )
    live = find_live_run(runs, pr_number, self_run_id)
    if live:
        return FenceDecision(
            proceed=False,
            reason=(
                "A loop is already running on this PR. Keeping the first one — "
                "it has the branch, and a second loop would push over it."
            ),
            live_run_id=live,
        )
    return FenceDecision(proceed=True, reason="ok")


def _gh_json(args: list[str], runner: Callable[..., Any] = subprocess.run) -> Any:
    proc = runner(["gh", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout or "null")


def main(argv: list[str] | None = None) -> int:
    repo = os.environ["REPO"]
    pr_number = os.environ["PR_NUMBER"]
    self_run_id = os.environ.get("RUN_ID", "")
    workflow = os.environ.get("WORKFLOW_FILE", "sdk-loop.yml")

    runs = (
        _gh_json(
            [
                "run",
                "list",
                "--repo",
                repo,
                "--workflow",
                workflow,
                "--limit",
                "40",
                "--json",
                "databaseId,status,displayTitle",
            ]
        )
        or []
    )

    decision = decide(
        os.environ.get("AUTHOR_ASSOCIATION"), runs, pr_number, self_run_id
    )
    if not decision.proceed:
        emit_outputs(
            proceed="false",
            reason=decision.reason,
            live_run_id=decision.live_run_id,
        )
        print(f"stand down: {decision.reason}", file=sys.stderr)
        # Exit 0: declining is a correct outcome, not a broken workflow. A red
        # run here would train people to ignore the lane's failures.
        return 0

    pr = _gh_json(
        [
            "pr",
            "view",
            pr_number,
            "--repo",
            repo,
            "--json",
            "headRefName,headRefOid,baseRefName,isDraft,state",
        ]
    )
    if pr.get("state") != "OPEN":
        emit_outputs(proceed="false", reason=f"PR is {pr.get('state')}, not open.")
        return 0

    emit_outputs(
        proceed="true",
        reason="ok",
        pr=pr_number,
        head_ref=pr["headRefName"],
        base_sha=pr["headRefOid"],
        base_ref=pr["baseRefName"],
        comment_id=os.environ.get("COMMENT_ID", ""),
    )
    print(f"baseline {pr['headRefOid'][:8]} on {pr['headRefName']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
