#!/usr/bin/env python3
"""Which `<!-- SDK_REVIEW -->` summary comments belong to THIS review run.

Two steps in `sdk-review.yml` ask that question and act on opposite answers:

    sdk_review_dedupe_verdicts.py   too many are ours  → minimize the extras
    sdk_review_verdict_gate.py      none are ours       → fail the run

A reader that says "ours" too readily lets another run's verdict vouch for a
run that delivered nothing; one that says "ours" too rarely fails a review that
did land. Getting those two wrong in opposite directions from two separate
implementations is how a green check and a red check end up describing the same
run, so the decision lives here once and both callers import it.

Attribution is by run URL, not by a time window. ORCHESTRATION.md §3e makes
`**Run:** …(<GHA_RUN_URL>)` mandatory on every summary, and the sandbox's own
replay guard (§6b) already greps it, so the key is exact: a replayed assistant
turn re-posts the same body and therefore the same URL, while another run's
summary can never carry ours.

A window cannot make that claim. The sandbox outlives a cancelled job and posts
minutes later, and a human trigger always passes the locked dedupe gate — so a
summary that is emphatically not ours can land between our starter comment and
our stream closing.

The window survives only as a fallback for summaries that name no run at all,
i.e. written before the footer existed. Anything naming another run is that
run's, whatever its timestamp.

Because the run URL carries the exact case, the window is free to be strict:
see `at_or_after`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# `sdk_review_approve.py` still accepts the legacy marker, and a gate that
# failed a run the approver would have stamped is worse than no gate.
SUMMARY_MARKERS = ("<!-- SDK_REVIEW -->", "<!-- TEST_SDK_REVIEW -->")

REVIEWED_HEAD_RE = re.compile(r"<!--\s*REVIEWED_HEAD:\s*([0-9a-f]{40})\s*-->")

# A GitHub Actions run URL, wherever it appears in a comment body. The trailing
# digits end the match, so the `)` closing a markdown link is excluded.
ACTIONS_RUN_URL_RE = re.compile(r"https://\S*?/actions/runs/\d+")

# What `attribute()` reports about how it decided.
BY_RUN_URL = "run-url"
BY_WINDOW = "window-fallback"
UNATTRIBUTED = "none"


def parse_ts(value: str) -> datetime | None:
    """A GitHub or JS ISO-8601 timestamp as an aware datetime, or None."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def at_or_after(created_at: str, since: datetime) -> bool:
    """Whether `created_at` is at or after `since`, compared as instants.

    The two sides differ in precision — GitHub records `created_at` to the
    second (`…:39Z`), the starter step stamps `new Date().toISOString()`
    (`…:39.371Z`) — so they are parsed rather than string-compared:
    lexicographically "Z" > ".", which would make `…:39Z >= …:39.371Z` true and
    let a summary from *before* the bound vouch for this run.

    Deliberately NOT floored to the second. Flooring would admit that
    pre-window summary back, and the failure it would guard against — this run's
    own verdict posted inside the starter's own second, stored a fraction
    earlier than the bound — cannot happen: the bound is our starter comment,
    and the sandbox has to boot and review before it can post. Nor would it
    matter if it did, because our own verdict is found by run URL and never
    reaches this comparison. Between a window that leans towards claiming
    someone else's summary and one that leans away, lean away.
    """
    created = parse_ts(created_at)
    if created is None:
        return False
    return created >= since


def is_summary(comment: dict) -> bool:
    body = comment.get("body") or ""
    return any(marker in body for marker in SUMMARY_MARKERS)


def claimed_run_urls(body: str) -> set[str]:
    """Every Actions run URL a comment body claims.

    Matched anywhere in the body rather than by parsing the `**Run:**` line,
    because that footer is LLM-rendered: a reworded link label must not read as
    "claims no run" and promote another run's summary into our fallback.
    """
    return set(ACTIONS_RUN_URL_RE.findall(body))


def _oldest_first(comments: list[dict]) -> list[dict]:
    # created_at is second-precision, so same-second duplicates tie; comment
    # ids increase monotonically and break the tie in posting order.
    return sorted(
        comments, key=lambda c: (str(c.get("created_at") or ""), c.get("id") or 0)
    )


def attribute(
    comments: list[dict],
    run_url: str,
    since: datetime | None = None,
    head_sha: str = "",
) -> tuple[list[dict], str]:
    """This run's summary comments (oldest first) and how they were identified.

    Returns `(summaries, BY_RUN_URL | BY_WINDOW | UNATTRIBUTED)`. Callers that
    write to the PR must act only on `BY_RUN_URL`: under the fallback we know a
    summary is not another run's, but not that it is ours.
    """
    summaries = [c for c in comments if is_summary(c)]

    if run_url:
        mine = [
            c for c in summaries if run_url in claimed_run_urls(c.get("body") or "")
        ]
        if mine:
            return _oldest_first(mine), BY_RUN_URL

    if since is None:
        return [], UNATTRIBUTED

    kept = []
    for comment in summaries:
        body = comment.get("body") or ""
        if claimed_run_urls(body):
            continue  # another run's — ours would have matched by URL above
        if not at_or_after(str(comment.get("created_at") or ""), since):
            continue
        stamped = REVIEWED_HEAD_RE.search(body)
        if stamped and head_sha and stamped.group(1) != head_sha:
            continue
        kept.append(comment)
    return (_oldest_first(kept), BY_WINDOW) if kept else ([], UNATTRIBUTED)
