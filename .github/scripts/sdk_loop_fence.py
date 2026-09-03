#!/usr/bin/env python3
"""Decide whether an `@sdk-loop` run may start, and pin its baseline.

Three jobs, in order, because each is cheaper than the next:

1. **Authorize.** Same bar as the existing lanes: OWNER / MEMBER / COLLABORATOR.
2. **Dismiss a duplicate.** If a loop is already running on this PR, the new
   one exits successfully having said so. It is NOT queued — by the time a
   queued run started, the live loop would have advanced the branch and the
   queued run would be answering a stale request. First run wins.
3. **Pin the baseline.** Record the head sha every later phase fences against.

Whatever it decides, it SAYS SO on the PR. A lane that answers `@sdk-loop`
with silence is indistinguishable from one that is broken, and the person who
typed it has no way to tell which — so both the stand-down and the start are
posted, with a link to the run.

Environment:
    GH_TOKEN            App installation token
    REPO                owner/name
    PR_NUMBER           the PR to drive
    COMMENT_ID          the triggering comment (for ANSWERS_TRIGGER)
    AUTHOR_ASSOCIATION  from the comment payload
    RUN_ID              this workflow run, so it can exclude itself
    GHA_RUN_URL         link included in whatever it posts
    WORKFLOW_FILE       e.g. sdk-loop.yml
    REVIEW_ONLY         'true' on a review-only dispatch: a closed or merged PR is
                        accepted, and the chain stops after Review 1
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


def admit_state(state: str | None, review_only: bool) -> str | None:
    """Why a PR in this state may not be driven — or None to proceed.

    The full loop refuses anything but OPEN: its resolver pushes, and a push
    to a merged or closed branch is work nobody asked for. A review-only run
    admits any state, because nothing after Review 1 runs — the review reads
    the diff against base, which a merged PR still has.
    """
    if state == "OPEN" or review_only:
        return None
    return f"the PR is {str(state).lower()}, not open."


MARK_START = "<!-- SDK_LOOP_STARTED -->"
MARK_DECLINE = "<!-- SDK_LOOP_DECLINED -->"


def start_comment(pr: str, run_url: str, sha: str, review_only: bool = False) -> str:
    if review_only:
        return (
            f"{MARK_START}\n"
            f"🔁 **`@sdk-loop`** review-only run on `{sha[:8]}`.\n\n"
            "One review, no fixes, no approval: the verdict is posted for "
            "comparison and nothing on this PR changes because of it.\n\n"
            f"[Watch it run]({run_url})"
        )
    return (
        f"{MARK_START}\n"
        f"🔁 **`@sdk-loop`** started on `{sha[:8]}`.\n\n"
        "It will review, fix what the review finds, and re-review until the "
        "findings are empty or it runs out of rounds or allowance. Each round "
        "posts its own verdict; a summary follows at the end.\n\n"
        f"[Watch it run]({run_url})"
    )


def decline_comment(reason: str, run_url: str, live_run_id: str = "") -> str:
    body = f"{MARK_DECLINE}\n🚫 **`@sdk-loop`** did not start — {reason}"
    if live_run_id:
        base = run_url.rsplit("/", 1)[0] if run_url else ""
        body += f"\n\nThe run that has the branch: {base}/{live_run_id}"
    return body


def post_comment(repo: str, pr: str, body: str, runner: Any = subprocess.run) -> None:
    """Best-effort. Failing to narrate must never fail the decision itself."""
    runner(
        ["gh", "pr", "comment", pr, "--repo", repo, "--body", body],
        capture_output=True,
        text=True,
        check=False,
    )


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
    run_url = os.environ.get("GHA_RUN_URL", "")

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
        post_comment(
            repo,
            pr_number,
            decline_comment(decision.reason, run_url, decision.live_run_id),
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
    # A review-only run is the A/B's instrument: it reviews a PR whose human
    # outcome is already known, so the PR is usually merged. That is safe
    # precisely because nothing after Review 1 runs — the resolver never sees
    # a branch it could push to. The full loop still refuses a closed PR:
    # pushing fixes to a merged branch is work nobody asked for.
    review_only = os.environ.get("REVIEW_ONLY", "").strip().lower() == "true"
    reason = admit_state(pr.get("state"), review_only)
    if reason:
        emit_outputs(proceed="false", reason=reason)
        post_comment(repo, pr_number, decline_comment(reason, run_url))
        return 0

    emit_outputs(
        proceed="true",
        reason="ok",
        pr=pr_number,
        head_ref=pr["headRefName"],
        base_sha=pr["headRefOid"],
        base_ref=pr["baseRefName"],
        comment_id=os.environ.get("COMMENT_ID", ""),
        # A string, like every other output, and defined on BOTH triggers. The
        # chain gates read this rather than `inputs.review_only` directly: on
        # an issue_comment run `inputs` is empty, and a dispatch boolean
        # arrives as the string 'false', which a bare `if:` reads as true.
        review_only="true" if review_only else "false",
    )
    post_comment(
        repo,
        pr_number,
        start_comment(pr_number, run_url, pr["headRefOid"], review_only=review_only),
    )
    print(f"baseline {pr['headRefOid'][:8]} on {pr['headRefName']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
