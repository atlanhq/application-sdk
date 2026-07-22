"""Tests for the pure scorecard scoring core (compute.build_scorecard).

Post tier-split the core takes a per-tier ``coverage`` map and a
``measured_tiers`` set (which tiers the run exercised); unit + integration are
always measured, e2e only when its junit was supplied.
"""

from __future__ import annotations

import pytest
from conformance.scorecard.compute import build_scorecard
from conformance.scorecard.rubric import Rubric, load_rubric
from conformance.scorecard.schema import (
    CoverageMetrics,
    RawTests,
    Scorecard,
    TierName,
    TierTestCounts,
)

_RUBRIC: Rubric = load_rubric("v1")
_COV = CoverageMetrics(lines_covered=86, lines_valid=100, percent=86.0)


def _build(
    tests: RawTests,
    coverage: dict[TierName, CoverageMetrics] | None = None,
    measured: set[TierName] | None = None,
) -> Scorecard:
    return build_scorecard(
        tests=tests,
        coverage=coverage
        if coverage is not None
        else {"unit": _COV, "integration": _COV},
        measured_tiers=measured if measured is not None else {"unit", "integration"},
        rubric=_RUBRIC,
        repo="atlanhq/atlan-mysql-app",
        app="mysql",
        commit_sha="abc123",
        tool_version="0.17.0",
        generated_at="2026-07-23T00:00:00Z",
    )


def _tier(sc: Scorecard, name: str):
    return next(t for t in sc.tiers if t.name == name)


def _gate(sc: Scorecard, gate_id: str):
    return next(g for g in sc.gates if g.id == gate_id)


def _check(sc: Scorecard, name: str, check_id: str):
    return next(c for c in _tier(sc, name).checks if c.id == check_id)


# ── e2e applicability (the headline behaviour) ───────────────────────────────


def test_e2e_absent_is_not_applicable_and_does_not_cap() -> None:
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=412, passed=412),
            integration=TierTestCounts(total=18, passed=18),
        ),
        measured={"unit", "integration"},
    )
    assert _tier(sc, "e2e").applicable is False
    assert _gate(sc, "e2e-present").status == "na"
    # grade is NOT capped at B — high unit+integration earns an A
    assert sc.aggregate.grade == "A"
    assert sc.aggregate.capped_by == []
    # but gold is unreachable without e2e
    assert sc.aggregate.maturity == "silver"


def test_e2e_present_and_green_reaches_gold() -> None:
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=10, passed=10),
            integration=TierTestCounts(total=5, passed=5),
            e2e=TierTestCounts(total=2, passed=2),
        ),
        coverage={
            "unit": CoverageMetrics(lines_covered=90, lines_valid=100, percent=90.0),
            "integration": CoverageMetrics(
                lines_covered=85, lines_valid=100, percent=85.0
            ),
        },
        measured={"unit", "integration", "e2e"},
    )
    assert _tier(sc, "e2e").applicable is True
    assert _gate(sc, "e2e-present").status == "pass"
    assert sc.aggregate.maturity == "gold"


def test_e2e_measured_but_skipped_fails_gate_not_na() -> None:
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=412, passed=412),
            integration=TierTestCounts(total=18, passed=18),
            e2e=TierTestCounts(total=3, skipped=3),
        ),
        measured={"unit", "integration", "e2e"},
    )
    assert _tier(sc, "e2e").applicable is True
    assert _tier(sc, "e2e").present is False
    assert _gate(sc, "e2e-present").status == "fail"
    # e2e now counts (0) toward the aggregate → score drops below B, maturity silver
    assert sc.aggregate.maturity == "silver"


def test_aggregate_renormalizes_over_applicable_tiers() -> None:
    """With e2e n/a, the aggregate is unit+integration renormalized to /100."""
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=10, passed=10),  # unit tier scores 100
            integration=TierTestCounts(total=10, passed=10),  # integration 100
        ),
        coverage={
            "unit": CoverageMetrics(lines_covered=100, lines_valid=100, percent=100.0),
            "integration": CoverageMetrics(
                lines_covered=100, lines_valid=100, percent=100.0
            ),
        },
        measured={"unit", "integration"},
    )
    # both tiers perfect → renormalized aggregate is exactly 100 (not 65)
    assert sc.aggregate.score == 100


# ── per-tier coverage ────────────────────────────────────────────────────────


def test_per_tier_coverage_scores_independently() -> None:
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=10, passed=10),
            integration=TierTestCounts(total=10, passed=10),
        ),
        coverage={
            "unit": CoverageMetrics(lines_covered=40, lines_valid=100, percent=40.0),
            "integration": CoverageMetrics(
                lines_covered=60, lines_valid=100, percent=60.0
            ),
        },
    )
    # target 80: unit 40/80=0.5, integration 60/80=0.75
    assert _check(sc, "unit", "unit.coverage").score == pytest.approx(0.5)
    assert _check(sc, "unit", "unit.coverage").value == "40.0%"
    assert _check(sc, "integration", "integration.coverage").score == pytest.approx(
        0.75
    )
    assert _check(sc, "integration", "integration.coverage").value == "60.0%"
    assert set(sc.raw.coverage.keys()) == {"unit", "integration"}


def test_missing_tier_coverage_scores_zero_na() -> None:
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=10, passed=10),
            integration=TierTestCounts(total=10, passed=10),
        ),
        coverage={"unit": _COV},  # no integration coverage
    )
    ic = _check(sc, "integration", "integration.coverage")
    assert ic.score == 0.0
    assert ic.value == "n/a"
    assert "integration" not in sc.raw.coverage


# ── gates / grade caps ───────────────────────────────────────────────────────


def test_no_unit_tests_caps_at_f() -> None:
    sc = _build(
        RawTests(integration=TierTestCounts(total=1, passed=1)),
        coverage={},
        measured={"unit", "integration"},
    )
    assert _gate(sc, "unit-present").status == "fail"
    assert sc.aggregate.grade == "F"
    assert sc.aggregate.maturity == "none"


def test_failing_test_caps_grade_at_c() -> None:
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=100, passed=98, failed=2),
            integration=TierTestCounts(total=18, passed=18),
        ),
    )
    assert sc.aggregate.grade == "C"
    assert "all-green" in sc.aggregate.capped_by
    assert _gate(sc, "all-green").status == "fail"
    assert sc.aggregate.maturity == "none"


def test_missing_integration_suite_counts_against() -> None:
    """No integration junit → integration is still measured, scored 0."""
    sc = _build(
        RawTests(unit=TierTestCounts(total=10, passed=10)),
        coverage={
            "unit": CoverageMetrics(lines_covered=90, lines_valid=100, percent=90.0)
        },
        measured={"unit", "integration"},
    )
    it = _tier(sc, "integration")
    assert it.applicable is True
    assert it.present is False
    assert it.score == 0
    assert sc.aggregate.maturity == "bronze"  # unit green, but no integration


def test_all_gates_always_reported() -> None:
    sc = _build(RawTests(unit=TierTestCounts(total=1, passed=1)))
    assert {g.id for g in sc.gates} == {"unit-present", "all-green", "e2e-present"}


def test_output_carries_provenance_and_per_tier_raw() -> None:
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=2, passed=2),
            integration=TierTestCounts(total=1, passed=1),
        )
    )
    assert sc.repo == "atlanhq/atlan-mysql-app"
    assert sc.rubric_version == "v1"
    assert sc.schema_version == "1.0"
    assert sc.raw.tests.unit.total == 2
    assert sc.raw.tests.integration.total == 1
