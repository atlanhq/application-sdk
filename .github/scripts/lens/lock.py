"""One lens review per PR at a time — a new request is dropped, never queued.

GitHub's `concurrency:` can only queue a new run behind a running one, or
cancel the RUNNING one; neither is "keep the review in progress, refuse the
newcomer". So each run is named `lens #<pr>` (`run-name:` in lens.yml) and,
before doing anything else, asks the Actions API whether an OLDER lens run
for the same PR is still queued or running. If one is, this run exits
straight away: no model calls, no state change, a one-line note on the PR.

Only the younger of two runs yields, so two requests landing at the same
instant still resolve to exactly one review — never zero, never two.
"""

from __future__ import annotations

from typing import Any

WORKFLOW_FILE = "lens.yml"
ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}


def run_name(pr: int) -> str:
    """Must match `run-name:` in .github/workflows/lens.yml."""
    return f"lens #{pr}"


def older_active_run(
    runs: list[dict[str, Any]], pr: int, own_run_id: int
) -> dict[str, Any] | None:
    """The run this one must yield to, or None when it may proceed."""
    name = run_name(pr)
    for r in runs:
        if r.get("id") == own_run_id or r.get("status") not in ACTIVE:
            continue
        if (r.get("display_title") or r.get("name")) != name:
            continue
        if int(r.get("id") or 0) < own_run_id:  # run ids increase monotonically
            return r
    return None


BUSY_NOTE = (
    "lens is already reviewing this PR ({url}), so this request was ignored. "
    "Comment `/lens` again once that review has posted."
)
