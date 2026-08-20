"""Tests for shared summary attribution — whose review comment is whose.

Two steps read this and act on opposite answers: the dedupe step minimizes the
extras when too many are ours, the delivery gate fails the run when none are.
Both failure directions are covered here because they are the same defect seen
from two sides.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "sdk_review_summaries",
    Path(__file__).resolve().parents[1] / "sdk_review_summaries.py",
)
summaries = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(summaries)

RUN_URL = "https://github.com/atlanhq/application-sdk/actions/runs/32286845311"
OTHER_RUN_URL = "https://github.com/atlanhq/application-sdk/actions/runs/32286821875"
HEAD = "4237280fd9dd1eb52c8b3a0eb44e4f4a3c2d1b0a"
OTHER_HEAD = "51c160b06a2a350289c7d779f4ab887503f98685"

# The starter step stamps `new Date().toISOString()`; GitHub's created_at has
# no sub-second part. Keeping the milliseconds here is the point.
SINCE = summaries.parse_ts("2026-08-19T18:09:39.371Z")
assert SINCE is not None


def verdict(
    comment_id: int,
    created_at: str,
    run_url: str | None = RUN_URL,
    head: str | None = HEAD,
    marker: str = "<!-- SDK_REVIEW -->",
) -> dict:
    """A summary comment as ORCHESTRATION.md §3e specifies it."""
    lines = [marker, "<!-- VERDICT: READY_TO_MERGE -->"]
    if head is not None:
        lines.append(f"<!-- REVIEWED_HEAD: {head} -->")
    lines += ["## SDK Review (mothership)", "", "---", "**CI:** all passing"]
    if run_url is not None:
        lines.append(f"**Run:** [view workflow logs + cost]({run_url})")
    return {"id": comment_id, "created_at": created_at, "body": "\n".join(lines)}


def ids(result: tuple[list[dict], str]) -> list[int]:
    return [c["id"] for c in result[0]]


# --- at_or_after() -------------------------------------------------------


def test_the_bounds_own_second_is_not_widened_into_the_window():
    """The window leans away from claiming a summary, not towards it.

    `…:39Z` parses to 39.000, before a `…:39.371Z` bound, so it stays out. A
    summary landing in the starter's own second is a *previous* run's — ours
    cannot exist yet, because the sandbox has not booted. And when ours does
    land, the run URL identifies it without consulting this window at all.
    """
    assert not summaries.at_or_after("2026-08-19T18:09:39Z", SINCE)


def test_string_comparison_would_have_admitted_it():
    """Pinning why this is parsed: as text, "Z" > "." and the guard inverts."""
    assert "2026-08-19T18:09:39Z" >= "2026-08-19T18:09:39.371Z"


def test_the_second_before_the_bound_is_excluded():
    assert not summaries.at_or_after("2026-08-19T18:09:38Z", SINCE)


def test_the_bound_is_inclusive():
    assert summaries.at_or_after("2026-08-19T18:09:39.371Z", SINCE)


def test_an_unparseable_timestamp_is_not_in_the_window():
    assert not summaries.at_or_after("not a timestamp", SINCE)


# --- claimed_run_urls() --------------------------------------------------


def test_the_footer_url_is_read_out_of_the_markdown_link():
    body = f"**Run:** [view workflow logs + cost]({RUN_URL})"
    assert summaries.claimed_run_urls(body) == {RUN_URL}


def test_the_closing_paren_is_not_part_of_the_url():
    assert RUN_URL in summaries.claimed_run_urls(f"[x]({RUN_URL})")


def test_a_body_naming_no_run_claims_nothing():
    assert summaries.claimed_run_urls("<!-- SDK_REVIEW -->\n## SDK Review") == set()


# --- attribute(): the exact tier ----------------------------------------


def test_our_run_url_identifies_our_summaries():
    comments = [
        verdict(1, "2026-08-19T17:33:00Z", run_url=OTHER_RUN_URL),
        {"id": 2, "created_at": "2026-08-19T18:10:00Z", "body": "@sdk-review"},
        verdict(3, "2026-08-19T18:14:35Z"),
    ]
    result = summaries.attribute(comments, RUN_URL, SINCE, HEAD)
    assert ids(result) == [3]
    assert result[1] == summaries.BY_RUN_URL


def test_the_exact_tier_ignores_the_window():
    """A replayed turn can post long after the starter; the URL still binds."""
    result = summaries.attribute([verdict(1, "2026-08-19T02:00:00Z")], RUN_URL, SINCE)
    assert ids(result) == [1]
    assert result[1] == summaries.BY_RUN_URL


def test_the_exact_tier_ignores_a_head_the_review_moved_to():
    """§6c lets a review update the branch, so REVIEWED_HEAD may not be ours."""
    comments = [verdict(1, "2026-08-19T18:14:35Z", head=OTHER_HEAD)]
    result = summaries.attribute(comments, RUN_URL, SINCE, HEAD)
    assert ids(result) == [1]


def test_same_second_duplicates_come_back_in_posting_order():
    comments = [
        verdict(30, "2026-08-19T18:14:50Z"),
        verdict(10, "2026-08-19T18:14:50Z"),
        verdict(20, "2026-08-19T18:14:50Z"),
    ]
    assert ids(summaries.attribute(comments, RUN_URL, SINCE)) == [10, 20, 30]


def test_the_legacy_marker_counts_as_a_summary():
    """`sdk_review_approve.py` accepts it, so refusing it here would red a
    run the approver would have stamped."""
    comments = [verdict(1, "2026-08-19T18:14:35Z", marker="<!-- TEST_SDK_REVIEW -->")]
    assert ids(summaries.attribute(comments, RUN_URL, SINCE)) == [1]


# --- attribute(): the fallback tier -------------------------------------


def test_another_runs_summary_is_never_ours():
    """The false-green: a zombie sandbox's verdict vouching for our silence.

    Its summary names another run, so no timestamp or head can make it ours.
    """
    comments = [verdict(1, "2026-08-19T18:14:35Z", run_url=OTHER_RUN_URL)]
    result = summaries.attribute(comments, RUN_URL, SINCE, HEAD)
    assert result == ([], summaries.UNATTRIBUTED)


def test_a_footerless_summary_in_our_window_for_our_head_is_admitted():
    comments = [verdict(1, "2026-08-19T18:14:35Z", run_url=None)]
    result = summaries.attribute(comments, RUN_URL, SINCE, HEAD)
    assert ids(result) == [1]
    assert result[1] == summaries.BY_WINDOW


def test_the_fallback_excludes_another_head():
    comments = [verdict(1, "2026-08-19T18:14:35Z", run_url=None, head=OTHER_HEAD)]
    assert summaries.attribute(comments, RUN_URL, SINCE, HEAD) == (
        [],
        summaries.UNATTRIBUTED,
    )


def test_the_fallback_admits_a_summary_predating_the_head_marker():
    comments = [verdict(1, "2026-08-19T18:14:35Z", run_url=None, head=None)]
    assert ids(summaries.attribute(comments, RUN_URL, SINCE, HEAD)) == [1]


def test_the_fallback_excludes_summaries_before_the_bound():
    comments = [verdict(1, "2026-08-19T17:33:00Z", run_url=None)]
    assert summaries.attribute(comments, RUN_URL, SINCE, HEAD) == (
        [],
        summaries.UNATTRIBUTED,
    )


def test_no_bound_and_no_url_match_attributes_nothing():
    comments = [verdict(1, "2026-08-19T18:14:35Z", run_url=None)]
    assert summaries.attribute(comments, RUN_URL, None) == ([], summaries.UNATTRIBUTED)


def test_no_run_url_at_all_falls_straight_through_to_the_window():
    """The delivery gate's older callers pass no URL; they must still work."""
    comments = [verdict(1, "2026-08-19T18:14:35Z", run_url=None)]
    result = summaries.attribute(comments, "", SINCE)
    assert ids(result) == [1]
    assert result[1] == summaries.BY_WINDOW


def test_non_summary_comments_are_never_attributed():
    comments = [
        {"id": 1, "created_at": "2026-08-19T18:14:35Z", "body": "lgtm"},
        {
            "id": 2,
            "created_at": "2026-08-19T18:14:36Z",
            "body": f"<!-- SDK_REVIEW_STARTED -->\n[run]({RUN_URL})",
        },
    ]
    assert summaries.attribute(comments, RUN_URL, SINCE) == ([], summaries.UNATTRIBUTED)
