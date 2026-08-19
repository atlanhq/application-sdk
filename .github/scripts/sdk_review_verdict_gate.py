#!/usr/bin/env python3
"""Fail the SDK review job when a completed run delivered no verdict.

A review that runs to `status=completed`, bills a real cost, and posts nothing
to the PR is the worst outcome the pipeline can produce: the check is green,
the bot is silent, and the only trace is a `::warning::` annotation nobody
reads. `sdk_review_approve.py` treats "no summary comment" as SKIPPED (a
legitimate no-op for the many other reasons a stamp is declined), so nothing
downstream turns that silence into a signal. This gate does.

Relationship to the dispatch step's soft-success rule (sdk-review.yml): that
rule exists for the opposite case — the summary WAS posted and mothership then
crashed in finalize/cleanup, so the stream broke after delivery (see the RCA
for run 29001242204). The two conditions are disjoint:

    soft-success : final_status != completed  AND  a verdict was posted
    this gate    : final_status == completed  AND  no verdict was posted

so this runs as an additional check rather than an edit to `fail_or_warn`.

Fail-open is deliberate everywhere the count cannot be established (comment
listing fails after retries, no PR number, no window lower bound). A false red
on a review that was in fact delivered is the failure mode the RCA above was
written about; a warning annotation plus a green check is the lesser harm when
we genuinely do not know.

Environment:
    REPO                 owner/repo (e.g. atlanhq/application-sdk)
    PR_NUMBER            pull request number
    FINAL_STATUS         the sandbox's terminal status from the dispatch step
    FINAL_COST           run cost, rendered in the PR comment (optional)
    STARTER_STARTED_AT   ISO-8601 lower bound: the starter comment's timestamp,
                         set by this run's own starter step. Comments older
                         than this belong to a previous trigger.
    GHA_RUN_URL          link to this workflow run, and the ownership key
                         `sdk_review_summaries.attribute()` matches on
    HEAD_SHA             the sha this run was dispatched for, so a summary for
                         a different head cannot vouch for this run
    GH_TOKEN             consumed by `gh` for auth (not read here directly)
    GITHUB_OUTPUT        path to the step-output file (optional)

Outputs:
    verdict_delivered   true | false | unknown
    summary_count       number of in-window summary comments (-1 when unknown)

Exit code:
    1  when a completed run posted no verdict (the defect this gate exists for)
    0  otherwise, including every fail-open path
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sdk_review_summaries import (  # noqa: E402  (needs the sys.path bootstrap)
    attribute,
    parse_ts,
)

# Our own marker. Deliberately not a `<!-- SDK_REVIEW -->` prefix so this
# comment can never be mistaken for a verdict by the counters above, by
# `sdk_review_approve.py`, or by `sdk_review_gate.py`.
NO_VERDICT_MARKER = "<!-- SDK_REVIEW_NO_VERDICT -->"

FETCH_ATTEMPTS = 3
FETCH_BACKOFF_S = 5.0

# A zero count is re-read before it is believed. The summary is posted in
# Phase 3, before the `complete` event, but the listing endpoint is not
# read-after-write consistent — a comment written seconds ago can be missing
# from the next GET. Confirming the zero costs 20s on the failure path only.
RECHECK_ATTEMPTS = 3
RECHECK_DELAY_S = 10.0

Runner = Callable[..., subprocess.CompletedProcess]
Sleeper = Callable[[float], None]


def fetch_comments(
    repo: str,
    pr_number: str,
    runner: Runner = subprocess.run,
    sleeper: Sleeper = time.sleep,
) -> list[dict] | None:
    """Every comment on the PR, or None when the query could not be completed.

    None (not []) on failure so callers can tell "no verdict" from "we could
    not look" — the difference between a red check and a fail-open warning.

    `--slurp` rather than `--jq`: `gh api` rejects the two together, and a
    slurped page array keeps multi-line bodies intact.
    """
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        result = runner(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--paginate",
                "--slurp",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            try:
                pages = json.loads(result.stdout or "[]")
            except json.JSONDecodeError as exc:
                print(f"Could not parse PR comments on attempt {attempt}: {exc}")
                pages = None
            if pages is not None:
                comments: list[dict] = []
                for page in pages:
                    # An un-paginated response is a bare array of comments;
                    # --slurp wraps it in one more level. Tolerate both.
                    if isinstance(page, list):
                        comments.extend(c for c in page if isinstance(c, dict))
                    elif isinstance(page, dict):
                        comments.append(page)
                return comments
        else:
            print(
                f"Could not list PR comments on attempt {attempt} "
                f"(exit {result.returncode}): {(result.stderr or '').strip()[:200]}"
            )
        if attempt < FETCH_ATTEMPTS:
            sleeper(FETCH_BACKOFF_S)
    return None


def count_summaries(
    comments: list[dict], since: datetime, run_url: str = "", head_sha: str = ""
) -> int:
    """How many summary comments this run posted.

    Delegates to `sdk_review_summaries.attribute()`, which the dedupe step also
    uses, so "this run posted no verdict" and "this run posted too many" are
    answered from one definition of ownership rather than two.

    That matters here more than anywhere: this gate is the only one that exits
    non-zero, so its count has to be the stricter of the two. Attributing by
    time window alone let another run's summary — a zombie sandbox posting
    minutes after its job was cancelled, or a concurrent human trigger — vouch
    for a run that delivered nothing.

    Every narrowing `attribute()` offers is passed, `head_sha` included.
    Dropping it left a footerless summary for a *different* reviewed head
    counting here while the dedupe step, which passed it, called the same
    comment nobody's — one shared decision returning two answers, which is the
    thing this module exists to prevent.
    """
    return len(attribute(comments, run_url, since, head_sha)[0])


def no_verdict_body(pr_number: str, cost: str, run_url: str) -> str:
    """The PR comment a silent run gets, so the requester learns it here."""
    lines = [
        NO_VERDICT_MARKER,
        "🟥 **SDK Review finished without posting a verdict.**",
        "",
        (
            f"The sandbox run for PR #{pr_number} reached `status=completed`"
            + (f" (cost `${cost}`)" if cost and cost != "unknown" else "")
            + " but no review summary comment reached this PR — so there is "
            "nothing to read and nothing was approved."
        ),
        "",
        "This is a failure of the review pipeline, not a statement about the code.",
        "",
        "Re-tag `@sdk-review` to run it again.",
    ]
    if run_url:
        lines += ["", f"[Workflow run that dropped the verdict]({run_url})"]
    return "\n".join(lines)


def post_comment(
    repo: str, pr_number: str, body: str, runner: Runner = subprocess.run
) -> bool:
    """Post `body` to the PR. Never raises: this runs on a path that is already
    failing the job, and a comment that could not be posted must not mask the
    red check that is the primary signal."""
    result = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/issues/{pr_number}/comments",
            "-X",
            "POST",
            "-f",
            f"body={body}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"::warning::could not post the no-verdict comment on PR #{pr_number} "
            f"(exit {result.returncode}): {(result.stderr or '').strip()[:200]}"
        )
        return False
    return True


def set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    line = f"{key}={value}\n"
    if path:
        with open(path, "a") as handle:
            handle.write(line)
    else:
        print(f"OUTPUT: {key}={value}")


def _fail_open(reason: str) -> int:
    print(f"::warning::verdict-delivery gate could not run: {reason} — not gating.")
    set_output("verdict_delivered", "unknown")
    set_output("summary_count", "-1")
    return 0


def main(runner: Runner = subprocess.run, sleeper: Sleeper = time.sleep) -> int:
    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    final_status = os.environ.get("FINAL_STATUS", "").strip()
    cost = os.environ.get("FINAL_COST", "").strip()
    run_url = os.environ.get("GHA_RUN_URL", "").strip()
    head_sha = os.environ.get("HEAD_SHA", "").strip()
    since = parse_ts(os.environ.get("STARTER_STARTED_AT", ""))

    # Only a run that claims it finished cleanly is in scope. Every other
    # terminal state is already adjudicated by the dispatch step's
    # `fail_or_warn`, which owns the soft-success rule for delivered-then-
    # dropped reviews. Re-deciding it here would risk contradicting it.
    if final_status != "completed":
        print(
            f"::notice::verdict-delivery gate: final_status='{final_status or 'none'}' "
            f"is not 'completed' — the dispatch step owns this outcome."
        )
        set_output("verdict_delivered", "unknown")
        set_output("summary_count", "-1")
        return 0

    if not repo or not pr_number:
        return _fail_open("no repo/PR number")
    if since is None:
        return _fail_open("no usable STARTER_STARTED_AT lower bound")

    count = 0
    for attempt in range(1, RECHECK_ATTEMPTS + 1):
        comments = fetch_comments(repo, pr_number, runner, sleeper)
        if comments is None:
            return _fail_open(f"could not list comments on PR #{pr_number}")
        count = count_summaries(comments, since, run_url, head_sha)
        if count > 0:
            break
        if attempt < RECHECK_ATTEMPTS:
            print(
                f"No in-window summary comment on PR #{pr_number} (attempt "
                f"{attempt}/{RECHECK_ATTEMPTS}) — re-reading in "
                f"{RECHECK_DELAY_S:.0f}s in case the listing has not caught up."
            )
            sleeper(RECHECK_DELAY_S)

    set_output("summary_count", str(count))

    if count > 0:
        set_output("verdict_delivered", "true")
        print(
            f"::notice::verdict-delivery gate: {count} SDK review summary "
            f"comment(s) posted on PR #{pr_number} since {since.isoformat()}."
        )
        return 0

    set_output("verdict_delivered", "false")
    post_comment(repo, pr_number, no_verdict_body(pr_number, cost, run_url), runner)
    print(
        f"::error::SDK Review completed (cost=${cost or 'unknown'}) but posted no "
        f"verdict comment on PR #{pr_number}. The run spent budget and delivered "
        f"nothing — failing so this is visible as a red check instead of a green "
        f"one. Re-tag @sdk-review to retry."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
