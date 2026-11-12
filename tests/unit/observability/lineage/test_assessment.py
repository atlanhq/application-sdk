"""Stage-1 verdicts, settling PENDING with lookup outcomes, and run counts."""

from __future__ import annotations

import pytest

from application_sdk.observability.lineage import (
    Assessment,
    CoverageCounts,
    CustomSqlParserFailure,
    GapCategory,
    LineageStatus,
    NonSqlSource,
    RefOutcome,
    UninstrumentedPath,
    UpstreamAmbiguous,
    UpstreamIdentityTooSparse,
    UpstreamNotInAtlan,
    Verdict,
    assess,
    settle,
)

M, X, A = RefOutcome.MATCHED, RefOutcome.MISSED, RefOutcome.AMBIGUOUS


def test_has_lineage_is_valid_with_no_gap():
    result = assess(LineageStatus.HAS_LINEAGE)
    assert result.verdict is Verdict.VALID
    assert result.gap is None


def test_missing_takes_the_reason_gap():
    result = assess(LineageStatus.MISSING_LINEAGE, CustomSqlParserFailure)
    assert result.verdict is Verdict.MISSING
    assert result.gap is GapCategory.PROCESSING
    assert result.to_row()["reasonCode"] == "CUSTOM_SQL_PARSER_FAILURE"


def test_missing_with_expected_reason_is_valid_none():
    result = assess(LineageStatus.MISSING_LINEAGE, NonSqlSource)
    assert result.verdict is Verdict.VALID_NONE
    assert result.gap is None
    assert result.basis == "by_design"


def test_missing_without_reason_is_uninstrumented():
    result = assess(LineageStatus.MISSING_LINEAGE)
    assert result.reason is UninstrumentedPath
    assert result.gap is None


def test_partial_is_invalid():
    result = assess(LineageStatus.PARTIAL_LINEAGE, UpstreamIdentityTooSparse)
    assert result.verdict is Verdict.INVALID
    assert result.gap is GapCategory.SOURCE


def test_pending_is_owned_by_asset_resolution():
    result = assess(LineageStatus.PENDING)
    assert result.verdict is Verdict.PENDING
    assert result.gap is GapCategory.ASSET_RESOLUTION


@pytest.mark.parametrize(
    ("outcomes", "verdict", "reason"),
    [
        ([M, M], Verdict.VALID, None),
        ([X, X], Verdict.MISSING, UpstreamNotInAtlan),
        ([M, X], Verdict.INVALID, UpstreamNotInAtlan),
        ([M, A], Verdict.INVALID, UpstreamAmbiguous),
        ([A], Verdict.INVALID, UpstreamAmbiguous),
    ],
)
def test_settle(outcomes, verdict, reason):
    result = settle(assess(LineageStatus.PENDING), outcomes)
    assert result.verdict is verdict
    assert result.reason is reason
    assert result.basis == "lookup"


def test_settle_waits_for_every_ref():
    pending = assess(LineageStatus.PENDING)
    assert settle(pending, [M, None]) is pending
    assert settle(pending, []) is pending


def test_settle_keeps_an_app_side_partial_failure():
    pending = assess(LineageStatus.PENDING, UpstreamIdentityTooSparse)
    result = settle(pending, [M, M])
    assert result.verdict is Verdict.INVALID
    assert result.reason is UpstreamIdentityTooSparse


def test_settle_leaves_settled_assets_alone():
    missing = assess(LineageStatus.MISSING_LINEAGE, CustomSqlParserFailure)
    assert settle(missing, [M]) is missing


def test_coverage_rate_excludes_pending_and_valid_none():
    counts = CoverageCounts()
    counts.extend(
        [
            assess(LineageStatus.HAS_LINEAGE),
            assess(LineageStatus.HAS_LINEAGE),
            assess(LineageStatus.HAS_LINEAGE),
            assess(LineageStatus.MISSING_LINEAGE, CustomSqlParserFailure),
            assess(LineageStatus.MISSING_LINEAGE, NonSqlSource),
            assess(LineageStatus.PENDING),
        ]
    )
    assert counts.total == 6
    assert counts.coverage_rate == 0.75
    assert counts.gaps == {GapCategory.PROCESSING: 1}
    assert counts.categories == {"PARSER_FAILURE": 1}


def test_coverage_rate_is_none_with_nothing_settled():
    counts = CoverageCounts()
    counts.add(Assessment(Verdict.PENDING))
    assert counts.coverage_rate is None
