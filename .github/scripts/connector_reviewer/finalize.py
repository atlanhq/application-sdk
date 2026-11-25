"""After a review run, whatever became of it: no head is left pending.

The review sets `pending` on the head with this Actions run's URL as the
status link before it calls a model, and replaces it with the verdict at the
end. A run that crashes, is cancelled or times out never gets to the end, and
lens leaves that `pending` (or an older green) standing. Here the workflow runs
`python -m connector_reviewer finalize` in an `if: always()` step after the
review step, and the review's own crash handler calls `finalize` too.

What it decides on is only what GitHub shows, so it is safe after any outcome:

- Which heads to look at: the pull request's current head, and the head this
  run set `pending` on. A push during the run moves the current head, so the
  review records the head it marked in a hidden marker on its own progress
  note (`progress_with_head`) the moment it sets `pending`; finalize reads it
  back from that note, which only the reviewer's App can write.
- A head whose latest `connector-reviewer` status is `pending` AND links to
  this run: this run started a review and never posted a verdict. It becomes
  `error` ("review incomplete"), which is never green.
- Anything else (a verdict, a skip, an exempt or waived head, a comment that was
  not `/review`, a run that found another review busy, or a pending status a
  newer run set) is left alone.
- A progress note this run posted and never removed is turned into the
  failure note, so it does not linger as "reviewing".

It makes no model call and reads no config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .github import STATUS_CONTEXT, GitHub, bot_login, recorded_verdicts
from .typed import EMPTY

PROGRESS_NOTE = (
    "⏳ **connector-reviewer is reviewing this PR** — [watch the run live]({url}).\n\n"
    "<sub>This note is removed when the verdict is posted. If it is still here after the run "
    "ends, the run was cancelled or timed out: see the log, then comment `/review` again.</sub>"
)
FAILED_NOTE = (
    "❌ **connector-reviewer failed before it could post a verdict** — see {url}. "
    "Comment `/review` to retry."
)
INCOMPLETE = (
    "review incomplete — the run ended without a verdict (crashed, cancelled or "
    "timed out); comment `/review` to retry"
)
_PROGRESS_START = PROGRESS_NOTE.split("{url}", 1)[0]
# The head a review set `pending` on, recorded on its progress note.
HEAD_MARKER = "<!-- connector-reviewer-head: {sha} -->"
_HEAD = re.compile(r"<!-- connector-reviewer-head: ([0-9A-Za-z]{1,64}) -->")


def progress_with_head(run_url: str, sha: str) -> str:
    """The progress note, with the head this run set `pending` on recorded in
    it for `finalize`."""
    return PROGRESS_NOTE.format(url=run_url) + "\n" + HEAD_MARKER.format(sha=sha)


@dataclass
class Finalized:
    head: str = ""  # the pull request's current head
    status_set: bool = False  # a pending status was turned into `error`
    errored: list[str] = field(default_factory=list)  # heads set to `error`
    notes_fixed: list[int] = field(default_factory=list)  # progress notes rewritten


def pr_number(event_name: str, event: dict[str, Any]) -> int:
    """The pull request an Actions event is about, or 0."""
    if event_name == "issue_comment":
        issue = event.get("issue") or EMPTY
        return int(issue.get("number") or 0) if "pull_request" in issue else 0
    if event_name in ("pull_request", "pull_request_target"):
        return int((event.get("pull_request") or EMPTY).get("number") or 0)
    if event_name == "workflow_dispatch":
        # A manual dispatch names the pull request in its `pr` input, as a
        # string. Without this, a cancelled dispatch left its status pending.
        raw = str((event.get("inputs") or EMPTY).get("pr") or "").strip().lstrip("#")
        return int(raw) if raw.isdigit() else 0
    return 0


def finalize(gh: GitHub, number: int, run_url: str) -> Finalized:
    """Turn this run's leftover `pending` into `error`, and its leftover
    progress note into the failure note. A no-op after a run that finished.

    `run_url` identifies this run: without it nothing can be attributed to
    this run, so nothing is changed."""
    out = Finalized()
    if not number or not run_url:
        return out
    pr = gh.pr(number) or EMPTY
    out.head = str((pr.get("head") or EMPTY).get("sha") or "")
    notes = [
        c
        for c in gh.issue_comments(number)
        if (c.get("user") or EMPTY).get("login") == bot_login()
        and str(c.get("body") or "").startswith(_PROGRESS_START)
        and run_url in str(c.get("body") or "")
    ]
    recorded = [m for c in notes for m in _HEAD.findall(str(c.get("body") or ""))]
    # A head this job already posted a verdict on is done, whatever the
    # combined status still shows: it can lag the verdict by seconds.
    posted = recorded_verdicts()
    for sha in dict.fromkeys(h for h in [out.head, *recorded] if h):
        if sha in posted:
            continue
        st = gh.status_of(sha, STATUS_CONTEXT) or EMPTY
        if st.get("state") == "pending" and st.get("target_url") == run_url:
            gh.set_status(sha, "error", INCOMPLETE, run_url)
            out.errored.append(sha)
    out.status_set = bool(out.errored)
    if posted:
        # The run reached a verdict and removes its own progress note; a
        # comment list read seconds later can still show it.
        return out
    for c in notes:
        gh.edit_comment(int(c["id"]), FAILED_NOTE.format(url=run_url))
        out.notes_fixed.append(int(c["id"]))
    return out
