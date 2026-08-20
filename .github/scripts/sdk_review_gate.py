#!/usr/bin/env python3
"""Decide whether an `@sdk-review` trigger should dispatch a review.

Every comment on a PR fires `sdk-review.yml`. Most are filtered by the job's
`if:`, but one case survives it and costs a full review run: the resolver
(`mothership-ai[bot]`) posts `@sdk-review` again on a HEAD that has already
been reviewed. The PR then carries two summaries for the same sha, written by
two runs, differing only in wording — indistinguishable to a reader from the
duplicate a crashed-and-replayed run produces.

Runs in two PHASEs against the same logic:

    preflight  the cheap `gate` job, OUTSIDE the per-PR concurrency group.
               An optimization: it turns away the common re-trigger before
               a VPN connect and a sandbox are spent on it.
    locked     the first step of `sdk-review-dispatch`, INSIDE the
               `sdk-review-$PR` concurrency group. This one is the
               authority: while it runs, no sibling run in the group can be
               executing, so the comment state it reads is post-lock and
               final. It adds the sibling-in-flight check on top of the
               unchanged-HEAD check, which covers the one case the lock does
               not — a sandbox that outlived its cancelled job (see
               `sdk-review.yml`, "Cancelling this job does NOT stop the
               sandbox") and is still going to post a verdict.

Both phases share `decide()`, so the two callers cannot drift on what counts
as a duplicate trigger. Before FND-636 the authoritative check lived in the
sandbox prompt (ORCHESTRATION.md Phase 0 §6b): the model had to boot and
reason before it could decline, so the run was paid for either way, and a
degraded model skipped the check entirely.

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
    GATE_PHASE      preflight (default) | locked
    HEAD_SHA        40-char sha this trigger would review. Optional: when
                    empty the sha is resolved here, with retries.
    EVENT_NAME      issue_comment | workflow_dispatch
    TRIGGER_ACTOR   login of the commenter ("" for workflow_dispatch)
    HEAD_RESOLVED   forces the fail-open path when set to "false". Normally
                    left unset — head resolution happens here.
    RUN_ID          this run's GITHUB_RUN_ID; a starter comment carrying it
                    is our own, not a sibling's (locked phase only)
    GH_TOKEN        consumed by `gh` for auth (not read here directly)
    GITHUB_OUTPUT   path to the step-output file (optional; falls back to stdout)

Outputs:
    decision       proceed | skip
    reason         machine-readable slug
    message        one line suitable for a PR comment or a ::notice::
    head_sha       the sha the decision was made against ("" if unresolved)
    head_resolved  true | false
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable

# Logins the workflow's `if:` lets through besides OWNER/MEMBER/COLLABORATOR
# humans. These are the automated re-triggers worth gating.
BOT_TRIGGERS = frozenset({"mothership-ai[bot]", "atlan-ci"})

SUMMARY_MARKER = "<!-- SDK_REVIEW -->"
REVIEWED_HEAD_RE = re.compile(r"<!--\s*REVIEWED_HEAD:\s*([0-9a-f]{40})\s*-->")

# The "review starting" comment the dispatch job posts before it hands off to
# the sandbox, and the two stamps it carries so a later run can tell whose it
# is and whether that run has finished.
STARTER_MARKER = "<!-- SDK_REVIEW_STARTED -->"
STARTER_HEAD_RE = re.compile(r"<!--\s*SDK_REVIEW_STARTED_HEAD:\s*([0-9a-f]{40})\s*-->")
STARTER_RUN_RE = re.compile(r"<!--\s*SDK_REVIEW_STARTED_RUN:\s*([0-9]+)\s*-->")
# Appended to the starter by the `Stamp cost + status onto starter comment`
# step, which is `always()` — so its presence means that run reached its end,
# cancelled or not. Kept byte-identical to the JS `includes('— status `')`
# guard in sdk-review.yml; changing one without the other makes every finished
# run look in-flight.
STARTER_STAMP = "\u2014 status `"

# Head resolution: 3 attempts, 5 s then 10 s backoff. Exhaustion is not fatal
# — see the fail-open note in main().
HEAD_ATTEMPTS = 3
HEAD_BACKOFF_SECONDS = 5

Runner = Callable[..., subprocess.CompletedProcess]
Sleeper = Callable[[float], None]


def resolve_head(
    repo: str,
    pr_number: str,
    runner: Runner = subprocess.run,
    sleeper: Sleeper = time.sleep,
) -> tuple[str, bool]:
    """The PR's current head sha, and whether we actually got it.

    Retried because a transient 5xx here used to decide the review: the
    caller had no sha, so the gate could not compare one, and the trigger was
    either dropped or duplicated depending on which way the caller guessed.
    On exhaustion we return ("", False) and main() proceeds fail-open.

    Lives here rather than as a shell `for` loop in the workflow both because
    docs/standards/ci.md forbids branching logic in YAML and because the
    locked phase needs the identical resolution — two copies of a retry loop
    is two things to keep in step.
    """
    for attempt in range(1, HEAD_ATTEMPTS + 1):
        result = runner(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}", "--jq", ".head.sha"],
            capture_output=True,
            text=True,
            check=False,
        )
        sha = (result.stdout or "").strip()
        if result.returncode == 0 and sha:
            return sha, True
        print(f"::warning::Head resolution attempt {attempt}/{HEAD_ATTEMPTS} failed")
        if attempt < HEAD_ATTEMPTS:
            sleeper(attempt * HEAD_BACKOFF_SECONDS)
    print(
        f"::warning::All {HEAD_ATTEMPTS} head-resolution attempts failed — "
        "gate will proceed fail-open"
    )
    return "", False


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


def inflight_sibling_run(
    comments: list[dict], head_sha: str, self_run_id: str
) -> str | None:
    """Run id of another run that claimed this sha and has not finished.

    A run claims a sha by posting the starter comment; the cost/status stamp
    is appended when the run ends. An unstamped starter from a run that is not
    us therefore means a review for this exact sha is still in flight.

    Under the concurrency lock that should be impossible — except the sandbox
    outlives a cancelled job, keeps working, and still posts its verdict with
    its own token minutes after GitHub calls the run cancelled. That zombie is
    what this catches; the lock covers everything else.

    Starters written before FND-636 carry no head/run stamps and are ignored,
    so rollout cannot skip a legitimate trigger on an in-flight PR.
    """
    if not head_sha:
        return None
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if not body.startswith(STARTER_MARKER):
            continue
        head = STARTER_HEAD_RE.search(body)
        if not head or head.group(1) != head_sha:
            continue
        run = STARTER_RUN_RE.search(body)
        if not run or run.group(1) == self_run_id:
            continue
        if STARTER_STAMP in body:
            continue  # that run reached its end
        return run.group(1)
    return None


def decide(
    event_name: str,
    actor: str,
    head_sha: str,
    reviewed_head: str | None,
    inflight_run: str | None = None,
) -> tuple[str, str, str]:
    """Return (decision, reason, message).

    `inflight_run` is only ever populated in the locked phase — the preflight
    gate runs outside the lock, where "a sibling is running" is the normal
    state and not grounds to decline.
    """
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
    if inflight_run:
        return (
            "skip",
            "sibling-run-in-flight",
            f"Skipping: run {inflight_run} is already reviewing `{head_sha[:7]}` and this "
            f"trigger came from @{actor}, not a human. Its verdict will land on this PR.",
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


def main(runner: Runner = subprocess.run, sleeper: Sleeper = time.sleep) -> int:
    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    event_name = os.environ.get("EVENT_NAME", "issue_comment")
    actor = os.environ.get("TRIGGER_ACTOR", "")
    phase = os.environ.get("GATE_PHASE", "preflight")
    self_run_id = os.environ.get("RUN_ID", "")

    # HEAD_SHA is an override for callers that already hold the sha; when it is
    # absent we resolve it (with retries) so both phases read the same value the
    # same way. HEAD_RESOLVED=false forces the fail-open branch below.
    head_sha = os.environ.get("HEAD_SHA", "")
    forced = os.environ.get("HEAD_RESOLVED", "")
    if forced == "false":
        head_sha, head_resolved = "", False
    elif head_sha:
        head_resolved = True
    else:
        head_sha, head_resolved = resolve_head(repo, pr_number, runner, sleeper)

    set_output("head_sha", head_sha)
    set_output("head_resolved", "true" if head_resolved else "false")

    # If all head-resolution retries were exhausted, proceed fail-open.
    # A missing review is always worse than an extra review.
    if not head_resolved:
        message = (
            f"Head SHA could not be resolved after {HEAD_ATTEMPTS} attempts "
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
    inflight_run: str | None = None
    # Only a bot trigger can be gated, so skip the API call entirely otherwise.
    if event_name == "issue_comment" and actor in BOT_TRIGGERS and repo and pr_number:
        comments = fetch_comments(repo, pr_number, runner)
        reviewed_head = last_reviewed_head(comments)
        if phase == "locked":
            inflight_run = inflight_sibling_run(comments, head_sha, self_run_id)

    decision, reason, message = decide(
        event_name, actor, head_sha, reviewed_head, inflight_run
    )

    set_output("decision", decision)
    set_output("reason", reason)
    set_output("message", message)
    print(f"::notice::sdk-review gate [{phase}]: {decision} ({reason}) — {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
