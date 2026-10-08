"""Unit tests for the auto-merged vs human-merged split (conformance.renovate.scan)."""

from __future__ import annotations

from conformance.renovate.scan import _AUTO_APPROVE_SIGNATURE, _auto_merge_stats


def _signed_review() -> dict:
    return {
        "state": "APPROVED",
        "body": f"{_AUTO_APPROVE_SIGNATURE} all required CI checks passed.",
        "author": {"login": "atlan-ci"},
    }


def test_bot_merger_counts_as_auto_merged_without_an_approval() -> None:
    # App repos no longer get an atlan-ci approval; the merger is the signal.
    stats = _auto_merge_stats(
        [{"mergedBy": {"login": "atlan-app-fleet", "is_bot": True}, "reviews": []}]
    )
    assert (stats.auto_merged, stats.human_merged) == (1, 0)


def test_signed_approval_still_counts_as_auto_merged() -> None:
    # application-sdk keeps its atlan-ci approval.
    stats = _auto_merge_stats(
        [
            {
                "mergedBy": {"login": "someone", "is_bot": False},
                "reviews": [_signed_review()],
            }
        ]
    )
    assert (stats.auto_merged, stats.human_merged) == (1, 0)


def test_person_merging_without_a_signature_is_human_merged() -> None:
    stats = _auto_merge_stats(
        [
            {"mergedBy": {"login": "someone", "is_bot": False}, "reviews": []},
            {"reviews": []},
        ]
    )
    assert (stats.auto_merged, stats.human_merged) == (0, 2)
