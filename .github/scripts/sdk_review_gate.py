#!/usr/bin/env python3
"""Decide whether an `@sdk-review` trigger should dispatch a review.

Every comment on a PR fires `sdk-review.yml`. Most are filtered by the job's
`if:`, but one case survives it and costs a full review run: the resolver
(`mothership-ai[bot]`) posts `@sdk-review` again on a HEAD that has already
been reviewed. The PR then carries two summaries for the same sha, written by
two runs, differing only in wording — indistinguishable to a reader from the
duplicate a crashed-and-replayed run produces.

A human tagging `@sdk-review` always dispatches, even on an unchanged HEAD:
re-reading the same diff is a legitimate thing to ask for, and second-guessing
it would make the tag feel broken.

If head resolution (the `gh api` call that fetches the PR's current HEAD SHA)
fails after all retries, we emit `decision=proceed` with
`reason=head-resolution-failed`. Reviewing is always the safe default; a
missing review is the harmful outcome.

Decision logic lives here rather than inlined in the workflow, per
docs/standards/ci.md.

Environment:
    REPO            owner/repo (e.g. atlanhq/application-sdk)
    PR_NUMBER       pull request number
    HEAD_SHA        40-char sha this trigger would review (empty when
                    HEAD_RESOLVED=false)
    EVENT_NAME      issue_comment | workflow_dispatch
    TRIGGER_ACTOR   login of the commenter ("" for workflow_dispatch)
    HEAD_RESOLVED   true (default) | false — set by the workflow when all
                    head-resolution retries are exhausted
    GH_TOKEN        consumed by `gh` for auth (not read here directly)
    GITHUB_OUTPUT   path to the step-output file (optional; falls back to stdout)

Outputs:
    decision  proceed | skip
    reason    machine-readable slug
    message   one line suitable for a PR comment or a ::notice::
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable

# Logins the workflow's `if:` lets through besides OWNER/MEMBER/COLLABORATOR
# humans. These are the automated re-triggers worth gating.
BOT_TRIGGERS = frozenset({"mothership-ai[bot]", "atlan-ci"})

SUMMARY_MARKER = "<!-- SDK_REVIEW -->"
REVIEWED_HEAD_RE = re.compile(r"<!--\s*REVIEWED_HEAD:\s*([0-9a-f]{40})\s*-->")

Runner = Callable[..., subprocess.CompletedProcess]


def fetch_comments(
    repo: str, pr_number: str, runner: Runner = subprocess.run
) -> list[dict]:
    """Every comment on the PR, or [] if the query fails.

    `--slurp` returns one array per page wrapped in an outer array; it is used
    instead of `--jq` deliberately. The naive `--jq '.[] | select(...) | .body'`
    form collapses a multiline body to its first line, which for a review
    summary is the HTML marker alone — the REVIEWED_HEAD stamp is on line 3 and
    would be silently dropped.
    """
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
    if result.returncode != 0:
        print(
            f"::warning::could not list PR comments (exit {result.returncode}) — not gating"
        )
        return []
    try:
        pages = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        print(f"::warning::could not parse PR comments ({e}) — not gating")
        return []
    comments: list[dict] = []
    for page in pages:
        # A single un-paginated response is a bare array of comments; --slurp
        # wraps it in one more level. Tolerate both shapes.
        if isinstance(page, list):
            comments.extend(c for c in page if isinstance(c, dict))
        elif isinstance(page, dict):
            comments.append(page)
    return comments


def last_reviewed_head(comments: list[dict]) -> str | None:
    """The REVIEWED_HEAD stamped by the most recent SDK review summary."""
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if SUMMARY_MARKER not in body:
            continue
        match = REVIEWED_HEAD_RE.search(body)
        return match.group(1) if match else None
    return None


def decide(
    event_name: str,
    actor: str,
    head_sha: str,
    reviewed_head: str | None,
) -> tuple[str, str, str]:
    """Return (decision, reason, message)."""
    if event_name != "issue_comment":
        return "proceed", "manual-dispatch", "Manual dispatch — reviewing."
    if actor not in BOT_TRIGGERS:
        return "proceed", "human-trigger", f"Human trigger by @{actor} — reviewing."
    if reviewed_head and head_sha and reviewed_head == head_sha:
        return (
            "skip",
            "unchanged-head-bot-retrigger",
            f"Skipping: `{head_sha[:7]}` already has an SDK review and this trigger came "
            f"from @{actor}, not a human. Push a commit, or tag `@sdk-review` yourself to "
            f"force a re-review.",
        )
    return (
        "proceed",
        "bot-trigger-new-head",
        f"Bot trigger by @{actor} on a new HEAD — reviewing.",
    )


def set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    line = f"{key}={value}\n"
    if path:
        with open(path, "a") as handle:
            handle.write(line)
    else:
        print(f"OUTPUT: {key}={value}")


def main(runner: Runner = subprocess.run) -> int:
    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    head_sha = os.environ.get("HEAD_SHA", "")
    event_name = os.environ.get("EVENT_NAME", "issue_comment")
    actor = os.environ.get("TRIGGER_ACTOR", "")
    head_resolved = os.environ.get("HEAD_RESOLVED", "true")

    # If all head-resolution retries were exhausted, proceed fail-open.
    # A missing review is always worse than an extra review.
    if head_resolved != "true":
        message = (
            "Head SHA could not be resolved after 3 attempts "
            "(transient GitHub API error). Proceeding fail-open — "
            "reviewing is the safe default."
        )
        set_output("decision", "proceed")
        set_output("reason", "head-resolution-failed")
        set_output("message", message)
        print(
            f"::notice::sdk-review gate: proceed (head-resolution-failed) — {message}"
        )
        return 0

    reviewed_head: str | None = None
    # Only a bot trigger can be gated, so skip the API call entirely otherwise.
    if event_name == "issue_comment" and actor in BOT_TRIGGERS and repo and pr_number:
        reviewed_head = last_reviewed_head(fetch_comments(repo, pr_number, runner))

    decision, reason, message = decide(event_name, actor, head_sha, reviewed_head)

    set_output("decision", decision)
    set_output("reason", reason)
    set_output("message", message)
    print(f"::notice::sdk-review gate: {decision} ({reason}) — {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
