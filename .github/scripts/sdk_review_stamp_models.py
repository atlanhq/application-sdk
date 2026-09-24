#!/usr/bin/env python3
"""Overwrite the `**Models:**` footer on this run's SDK review summary.

The footer used to be written by the reviewer itself, from a prompt asking it
to name "the models that actually ran this review". It cannot know: model
routing happens in mothership and the gateway, outside anything the sandboxed
agent can observe, so it filled the line from its own Claude Code context and
posted reviews claimed Claude models on runs pinned entirely to non-Claude
ones.

The dispatch step does know. It reads the raw CLI frames mothership forwards,
and each carries the model the API reported answering (see
`sdk_review_dispatch.frame_models`). This step writes that list over whatever
the reviewer put there, or inserts the line if it is missing.

Only summaries attributed to this run BY RUN URL are touched — the rule
`sdk_review_summaries.attribute()` sets for every caller that writes to the PR.
The window fallback cannot prove a comment is ours.

Fail-open everywhere: a footer that could not be corrected is a cosmetic
defect, never a reason to red a review that was delivered.

Environment:
    REPO                 owner/repo
    PR_NUMBER            pull request number
    GHA_RUN_URL          this run's Actions URL — the attribution key
    MODELS_USED          the dispatch step's `models_used` output
    GH_TOKEN             consumed by `gh` for auth (not read here directly)

Exit code: always 0.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sdk_review_summaries import BY_RUN_URL, attribute  # noqa: E402
from sdk_review_verdict_gate import (  # noqa: E402  (needs the sys.path bootstrap)
    RECHECK_ATTEMPTS,
    RECHECK_DELAY_S,
    Runner,
    Sleeper,
    fetch_comments,
)

# The whole footer line, whatever the reviewer wrote after the label.
MODELS_LINE_RE = re.compile(r"^\*\*Models:\*\*.*$", re.MULTILINE)
RUN_LINE_RE = re.compile(r"^\*\*Run:\*\*", re.MULTILINE)

# What the footer says when the stream showed no model at all. Saying so is
# the point: leaving the reviewer's guess in place is the defect being fixed.
NOT_REPORTED = "not reported by the run stream"


def models_line(models: str) -> str:
    return f"**Models:** {models.strip() or NOT_REPORTED}"


def stamp(body: str, models: str) -> str | None:
    """`body` with its Models footer set to `models`, or None if unchanged.

    Replaces the LAST `**Models:**` line — the footer. A re-review carries the
    prior summary into its delta section, so an earlier footer can be quoted
    above the real one; the first match would "fix" the quote and leave the
    guess standing. With no such line, one is inserted directly above the last
    `**Run:**` line. A body with neither is left alone — there is no footer to
    anchor to, and guessing a position risks landing inside the review text.
    Only that one line changes, so the markers, `REVIEWED_HEAD` and the run URL
    the approver, the dedupe step and the verdict gate key on stay byte-identical.
    """
    line = models_line(models)
    footers = list(MODELS_LINE_RE.finditer(body))
    if footers:
        last = footers[-1]
        new = body[: last.start()] + line + body[last.end() :]
    else:
        runs = list(RUN_LINE_RE.finditer(body))
        if not runs:
            return None
        at = runs[-1].start()
        new = body[:at] + line + "\n" + body[at:]
    return new if new != body else None


def patch_comment(
    repo: str, comment_id: int, body: str, runner: Runner = subprocess.run
) -> bool:
    result = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/issues/comments/{comment_id}",
            "-X",
            "PATCH",
            "-f",
            f"body={body}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"::warning::could not correct the Models footer on comment "
            f"{comment_id} (exit {result.returncode}): "
            f"{(result.stderr or '').strip()[:200]}"
        )
        return False
    return True


def main(runner: Runner = subprocess.run, sleeper: Sleeper = time.sleep) -> int:
    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    run_url = os.environ.get("GHA_RUN_URL", "").strip()
    models = os.environ.get("MODELS_USED", "").strip()

    if not repo or not pr_number or not run_url:
        print("::notice::Models footer not stamped: no repo, PR number or run URL.")
        return 0

    mine: list[dict] = []
    for attempt in range(1, RECHECK_ATTEMPTS + 1):
        comments = fetch_comments(repo, pr_number, runner, sleeper)
        if comments is None:
            print(
                f"::warning::Models footer not stamped: could not list PR #{pr_number} comments."
            )
            return 0
        found, how = attribute(comments, run_url)
        if how == BY_RUN_URL:
            mine = found
            break
        # The listing is not read-after-write consistent; a summary posted
        # seconds ago can be missing from this read.
        if attempt < RECHECK_ATTEMPTS:
            sleeper(RECHECK_DELAY_S)

    if not mine:
        print(
            f"::notice::Models footer not stamped: no summary on PR #{pr_number} names {run_url}."
        )
        return 0

    for comment in mine:
        new = stamp(comment.get("body") or "", models)
        if new is None:
            continue
        if patch_comment(repo, comment["id"], new, runner):
            print(f"Stamped {models_line(models)!r} on comment {comment['id']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
