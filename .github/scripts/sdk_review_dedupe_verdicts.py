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

**Ownership is decided by the run URL, not by a time window.** Every summary
carries `**Run:** …(<GHA_RUN_URL>)` — required by ORCHESTRATION.md §3e and
already load-bearing for the sandbox's own replay guard (§6b) — so matching it
is exact. A time window is not: a zombie sandbox that outlived its cancelled job
posts minutes later, and a human-triggered run always passes the locked gate, so
a summary belonging to someone else can land inside our window. Counting it as
ours would minimize our own verdict as the DUPLICATE, or report a delivered
review when our sandbox never posted one.

If no summary carries our run URL we fall back to the window, narrowed to
summaries whose `REVIEWED_HEAD` is ours (or absent), and minimize NOTHING —
without exact attribution, hiding a comment risks hiding someone else's review.

Also reports whether a verdict was posted at all, which is what the dispatch
step's soft-success rule turns on — a run whose stream broke *after* the
summary landed has delivered its review and must not fail the PR check.

Environment:
    REPO             owner/repo
    PR_NUMBER        pull request number
    GHA_RUN_URL      this run's Actions run URL — the ownership key
    HEAD_SHA         the sha this run was dispatched for, for the fallback
    SINCE            ISO8601 lower bound for the fallback only. Floored to
                     whole seconds before comparing: GitHub records
                     `created_at` to the second, so an unfloored
                     `…:39.900Z` bound excludes a verdict posted at
                     `…:39.950Z` and stored as `…:39Z`.
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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sdk_review_gate import (  # noqa: E402  (needs the sys.path bootstrap)
    REVIEWED_HEAD_RE,
    SUMMARY_MARKER,
    Runner,
    fetch_comments,
)

MINIMIZE_MUTATION = (
    "mutation($id: ID!) { minimizeComment(input: {subjectId: $id, "
    "classifier: DUPLICATE}) { minimizedComment { isMinimized } } }"
)

Writer = Callable[[str, str], None]


def _parsed(stamp: str) -> datetime | None:
    """`2026-08-19T18:09:39Z` / `…39.123Z` as an aware datetime, or None."""
    try:
        return datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def created_at_or_after(stamp: str, bound: str) -> bool:
    """Whether `stamp` is at or after `bound`, both floored to the second.

    Parsed rather than string-compared because the two sides are produced by
    different writers with different precision: GitHub stamps comments to the
    second (`…:39Z`) while the starter step records `new Date().toISOString()`
    (`…:39.123Z`). That mismatch bites in both directions, so the comparison
    discards sub-second precision on both sides:

    - lexicographically "Z" > ".", so `…:39Z >= …:39.123Z` is True as strings
      and a comment created *before* the bound would count as ours;
    - as instants, a verdict posted at `…:39.950Z` is stored as `…:39Z` and
      parses to `39.000`, which is *before* a `…:39.900Z` bound — excluding the
      verdict this run just posted, reporting no verdict, and hard-failing a
      review that was delivered.
    """
    left, right = _parsed(stamp), _parsed(bound)
    if left is None or right is None:
        return stamp >= bound  # last resort; both shapes are ISO8601 in practice
    return left.replace(microsecond=0) >= right.replace(microsecond=0)


def _oldest_first(comments: list[dict]) -> list[dict]:
    # created_at has second precision, so same-second duplicates tie; comment
    # ids increase monotonically and break the tie in posting order.
    return sorted(
        comments, key=lambda c: (str(c.get("created_at") or ""), c.get("id") or 0)
    )


def _summaries(comments: list[dict]) -> list[dict]:
    return [c for c in comments if SUMMARY_MARKER in (c.get("body") or "")]


def summaries_by_run_url(comments: list[dict], run_url: str) -> list[dict]:
    """Summaries this run posted, identified exactly, oldest first.

    ORCHESTRATION.md §3e makes `**Run:** …(<GHA_RUN_URL>)` mandatory on every
    summary, so the run URL is an exact ownership key — a replayed turn re-posts
    the same body and therefore the same URL, while another run's summary can
    never carry ours.
    """
    if not run_url:
        return []
    return _oldest_first(
        [c for c in _summaries(comments) if run_url in (c.get("body") or "")]
    )


def summaries_in_window(comments: list[dict], since: str, head_sha: str) -> list[dict]:
    """Fallback attribution: our window, narrowed to our reviewed head.

    Only reached when no summary carries our run URL, i.e. the sandbox omitted
    the footer §3e requires. The head narrowing is what keeps a zombie
    sandbox's summary for an older sha out of the count; a summary with no
    `REVIEWED_HEAD` at all predates that marker and is admitted rather than
    hard-failing a review that may well have been delivered.
    """
    kept = []
    for comment in _summaries(comments):
        if not created_at_or_after(str(comment.get("created_at") or ""), since):
            continue
        stamped = REVIEWED_HEAD_RE.search(comment.get("body") or "")
        if stamped and head_sha and stamped.group(1) != head_sha:
            continue
        kept.append(comment)
    return _oldest_first(kept)


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
    summaries = summaries_by_run_url(comments, run_url)
    attribution = "run-url"
    if not summaries:
        # No summary carries our run URL. Either we posted nothing, or the
        # sandbox dropped the footer §3e requires. Either way attribution is
        # inexact from here on, so we report but never hide.
        summaries = summaries_in_window(comments, since, head_sha) if since else []
        attribution = "window-fallback" if summaries else "none"
        if summaries:
            print(
                f"::warning::{len(summaries)} SDK review summary comment(s) fall in "
                f"this run's window but none carries this run's URL ({run_url}); the "
                "sandbox likely dropped the required **Run:** footer "
                "(ORCHESTRATION.md §3e). Counting them for the soft-success rule but "
                "minimizing nothing — without exact attribution a duplicate cannot be "
                "told from another run's review."
            )

    minimized = 0

    if attribution == "run-url" and len(summaries) > 1:
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
