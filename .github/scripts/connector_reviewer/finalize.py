"""After a review run, whatever became of it: no head is left pending.

The review sets `pending` on the head with this Actions run's URL as the
status link before it calls a model, and replaces it with the verdict at the
end. A run that crashes, is cancelled or times out never gets to the end, and
lens leaves that `pending` (or an older green) standing. Here the workflow runs
`python -m connector_reviewer finalize` in an `if: always()` step after the
review step, and the review's own crash handler calls `finalize` too.

What it decides on is only what GitHub shows, so it is safe after any outcome:

- The head's latest `connector-reviewer` status is `pending` AND links to this
  run: this run started a review and never posted a verdict. It becomes `error`
  ("review incomplete"), which is never green.
- Anything else (a verdict, a skip, an exempt or waived head, a comment that was
  not `/review`, a run that found another review busy, or a pending status a
  newer run set) is left alone.
- A progress note this run posted and never removed is turned into the
  failure note, so it does not linger as "reviewing".

It makes no model call and reads no config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .github import STATUS_CONTEXT, GitHub, bot_login
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


@dataclass
class Finalized:
    head: str = ""
    status_set: bool = False  # the pending status was turned into `error`
    notes_fixed: list[int] = field(default_factory=list)  # progress notes rewritten


def pr_number(event_name: str, event: dict[str, Any]) -> int:
    """The pull request an Actions event is about, or 0."""
    if event_name == "issue_comment":
        issue = event.get("issue") or EMPTY
        return int(issue.get("number") or 0) if "pull_request" in issue else 0
    if event_name in ("pull_request", "pull_request_target"):
        return int((event.get("pull_request") or EMPTY).get("number") or 0)
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
    if out.head:
        st = gh.status_of(out.head, STATUS_CONTEXT) or EMPTY
        if st.get("state") == "pending" and st.get("target_url") == run_url:
            gh.set_status(out.head, "error", INCOMPLETE, run_url)
            out.status_set = True
    for c in gh.issue_comments(number):
        body = str(c.get("body") or "")
        if (
            (c.get("user") or EMPTY).get("login") == bot_login()
            and body.startswith(_PROGRESS_START)
            and run_url in body
        ):
            gh.edit_comment(int(c["id"]), FAILED_NOTE.format(url=run_url))
            out.notes_fixed.append(int(c["id"]))
    return out
