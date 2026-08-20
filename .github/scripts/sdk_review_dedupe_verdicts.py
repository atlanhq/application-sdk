#!/usr/bin/env python3
"""Collapse duplicate `<!-- SDK_REVIEW -->` verdict comments from one run.

A review run posts exactly one summary. On 2026-08-19 PR #3276 received the
identical summary five times in 76 seconds (FND-636): a provider-level retry
replays the assistant turn, and the tool call in that turn — `gh api …
/comments -f body=…` — is not idempotent, so it posts again. The frequency of
that is model-dependent; the vulnerability is not, which is why the fix is
post-hoc and deterministic rather than another instruction in the prompt.

So: after the sandbox stream closes, find the summaries this run posted. One is
the normal case. More than one means a replay, and every copy but the newest is
minimized as a duplicate (GraphQL `minimizeComment`) with a warning annotation.
Bodies are never edited, and the newest copy stays visible so
`last_reviewed_head()` and `sdk_review_approve.py` keep reading the verdict they
read before.

Minimizing rather than deleting is deliberate: the duplicates are evidence of a
mothership-side replay, and a reader chasing "why did I get five emails" needs
to be able to expand them.

Ownership is decided in `sdk_review_summaries.attribute()`, shared with
`sdk_review_verdict_gate.py` so the step that minimizes extras and the step that
fails on none cannot disagree about whose summary a comment is. Read that module
for why the run URL is the key and the time window is only a fallback.

Under the fallback we minimize NOTHING: it establishes that a summary is not
another run's, not that it is ours, and hiding a comment on that basis risks
hiding someone else's review.

Also reports whether a verdict was posted at all, which is what the dispatch
step's soft-success rule turns on — a run whose stream broke *after* the
summary landed has delivered its review and must not fail the PR check.

Environment:
    REPO             owner/repo
    PR_NUMBER        pull request number
    GHA_RUN_URL      this run's Actions run URL — the ownership key
    HEAD_SHA         the sha this run was dispatched for, for the fallback
    SINCE            ISO8601 lower bound, used only by the fallback tier
    DEDUPE_OUTPUT    path to write `key=value` results to (optional; falls
                     back to GITHUB_OUTPUT, then stdout)
    DRY_RUN          "true" to report without minimizing
    GH_TOKEN         consumed by `gh` for auth (not read here directly)

Outputs:
    verdict_posted        1 if this run posted at least one summary, else 0
    verdict_count         how many summaries this run posted
    minimized_count       how many were minimized
    attribution           run-url | window-fallback | none

Always exits 0. This runs between the sandbox finishing and the dispatch step
deciding pass/fail; turning a tidy-up failure into a red review check would
trade a cosmetic problem for a real one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sdk_review_gate import (  # noqa: E402  (needs the sys.path bootstrap)
    Runner,
    fetch_comments,
)
from sdk_review_summaries import (  # noqa: E402  (needs the sys.path bootstrap)
    BY_RUN_URL,
    attribute,
    parse_ts,
)

MINIMIZE_MUTATION = (
    "mutation($id: ID!) { minimizeComment(input: {subjectId: $id, "
    "classifier: DUPLICATE}) { minimizedComment { isMinimized } } }"
)

Writer = Callable[[str, str], None]


def minimize(node_id: str, runner: Runner = subprocess.run) -> bool:
    """Hide one comment as a duplicate. False if GitHub refused."""
    result = runner(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={MINIMIZE_MUTATION}",
            "-f",
            f"id={node_id}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"::warning::could not minimize duplicate verdict comment {node_id} "
            f"(exit {result.returncode}): {(result.stderr or '').strip()[:200]}"
        )
        return False
    return True


def make_writer() -> Writer:
    path = os.environ.get("DEDUPE_OUTPUT") or os.environ.get("GITHUB_OUTPUT")

    def _write(key: str, value: str) -> None:
        line = f"{key}={value}\n"
        if path:
            with open(path, "a") as handle:
                handle.write(line)
        else:
            print(f"OUTPUT: {key}={value}")

    return _write


def main(runner: Runner = subprocess.run) -> int:
    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    run_url = os.environ.get("GHA_RUN_URL", "").strip()
    head_sha = os.environ.get("HEAD_SHA", "").strip()
    since = os.environ.get("SINCE", "").strip()
    dry_run = os.environ.get("DRY_RUN", "") == "true"
    write = make_writer()

    if not (repo and pr_number and (run_url or since)):
        # Nothing to attribute by. Report nothing found and touch nothing.
        print(
            "::notice::sdk-review verdict dedupe: skipped "
            f"(repo={repo or 'unset'} pr={pr_number or 'unset'} "
            f"run_url={run_url or 'unset'} since={since or 'unset'})"
        )
        write("verdict_posted", "0")
        write("verdict_count", "0")
        write("minimized_count", "0")
        write("attribution", "none")
        return 0

    comments = fetch_comments(repo, pr_number, runner)
    summaries, attribution = attribute(comments, run_url, parse_ts(since), head_sha)
    if attribution != BY_RUN_URL and summaries:
        # No summary carries our run URL, so the sandbox dropped the footer §3e
        # requires. We can say these are nobody else's, not that they are ours —
        # report them for the soft-success rule, but hide nothing.
        print(
            f"::warning::{len(summaries)} SDK review summary comment(s) fall in this "
            f"run's window but none carries this run's URL ({run_url}); the sandbox "
            "likely dropped the required **Run:** footer (ORCHESTRATION.md §3e). "
            "Counting them for the soft-success rule but minimizing nothing — without "
            "exact attribution a duplicate cannot be told from another run's review."
        )

    minimized = 0

    if attribution == BY_RUN_URL and len(summaries) > 1:
        newest = summaries[-1]
        stale = summaries[:-1]
        print(
            f"::warning::This run posted {len(summaries)} SDK review summaries on "
            f"PR #{pr_number}; one is expected. Keeping comment {newest.get('id')} "
            f"and minimizing {len(stale)} duplicate(s) — the sandbox turn that posts "
            "the summary was replayed (FND-636)."
        )
        for comment in stale:
            node_id = comment.get("node_id") or ""
            if not node_id:
                print(
                    f"::warning::duplicate verdict comment {comment.get('id')} has no "
                    "node_id — cannot minimize it"
                )
                continue
            if dry_run:
                minimized += 1
                continue
            if minimize(node_id, runner):
                minimized += 1

    write("verdict_posted", "1" if summaries else "0")
    write("verdict_count", str(len(summaries)))
    write("minimized_count", str(minimized))
    write("attribution", attribution)
    print(
        f"::notice::sdk-review verdict dedupe: {len(summaries)} summary comment(s) "
        f"from this run (attribution={attribution}), {minimized} minimized."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
