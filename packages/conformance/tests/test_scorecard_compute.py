"""Tests for the pure scorecard scoring core (compute.build_scorecard)."""

from __future__ import annotations

import pytest
from conformance.scorecard.compute import build_scorecard
from conformance.scorecard.rubric import Rubric, load_rubric
from conformance.scorecard.schema import (
    CoverageMetrics,
    RawTests,
    Scorecard,
    TierTestCounts,
)

_RUBRIC: Rubric = load_rubric("v1")
_FULL_COV = CoverageMetrics(
    lines_covered=860, lines_valid=1000, percent=86.0, branch_percent=78.4
)


def _build(tests: RawTests, coverage: CoverageMetrics | None = _FULL_COV) -> Scorecard:
    return build_scorecard(
        tests=tests,
        coverage=coverage,
        rubric=_RUBRIC,
        repo="atlanhq/atlan-mysql-app",
        app="mysql",
        commit_sha="abc123",
        tool_version="0.13.0",
        generated_at="2026-07-21T12:00:00Z",
    )


def _tier(sc: Scorecard, name: str):
    return next(t for t in sc.tiers if t.name == name)


def _gate(sc: Scorecard, gate_id: str):
    return next(g for g in sc.gates if g.id == gate_id)


def _check(sc: Scorecard, name: str, check_id: str):
    return next(c for c in _tier(sc, name).checks if c.id == check_id)


def test_healthy_app_is_gold_a() -> None:
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=412, passed=412),
            integration=TierTestCounts(total=18, passed=18),
            e2e=TierTestCounts(total=3, passed=3),
        )
    )
    assert sc.aggregate.score == 100
    assert sc.aggregate.grade == "A"
    assert sc.aggregate.maturity == "gold"
    assert sc.aggregate.capped_by == []
    assert all(g.status == "pass" for g in sc.gates)


def test_e2e_collect_and_skip_is_not_present() -> None:
    """The load-bearing gap-fill: skipped e2e reads as absent, not green."""
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=412, passed=412),
            integration=TierTestCounts(total=18, passed=18),
            e2e=TierTestCounts(total=3, skipped=3),
        )
    )
    assert _tier(sc, "e2e").present is False
    assert _tier(sc, "e2e").score == 0
    assert _gate(sc, "e2e-present").status == "fail"
    # score already below B because e2e contributes 0 → maturity capped at silver
    assert sc.aggregate.maturity == "silver"


def test_no_unit_tests_caps_at_f() -> None:
    sc = _build(
        RawTests(e2e=TierTestCounts(total=1, passed=1)),
        coverage=None,
    )
    assert _gate(sc, "unit-present").status == "fail"
    assert sc.aggregate.grade == "F"
    assert sc.aggregate.maturity == "none"


def test_failing_test_caps_grade_at_c_and_blocks_maturity() -> None:
    sc = _build(
        RawTests(
            unit=TierTestCounts(total=100, passed=98, failed=2),
            integration=TierTestCounts(total=18, passed=18),
            e2e=TierTestCounts(total=3, passed=3),
        )
    )
    # raw weighted score is ~A, but all-green fails → capped to C
    assert sc.aggregate.grade == "C"
    assert "all-green" in sc.aggregate.capped_by
    assert _gate(sc, "all-green").status == "fail"
    # a non-green unit tier cannot even be bronze
    assert sc.aggregate.maturity == "none"


def test_coverage_normalizes_against_target() -> None:
    # 40% coverage against an 80% target → coverage check score 0.5
    sc = _build(
        RawTests(unit=TierTestCounts(total=10, passed=10)),
        coverage=CoverageMetrics(lines_covered=40, lines_valid=100, percent=40.0),
    )
    cov_check = _check(sc, "unit", "unit.coverage")
    assert cov_check.score == pytest.approx(0.5)
    assert cov_check.value == "40.0%"


def test_coverage_above_target_clamps_to_one() -> None:
    sc = _build(
        RawTests(unit=TierTestCounts(total=10, passed=10)),
        coverage=CoverageMetrics(lines_covered=95, lines_valid=100, percent=95.0),
    )
    assert _check(sc, "unit", "unit.coverage").score == pytest.approx(1.0)


def test_missing_coverage_scores_zero_with_na_value() -> None:
    sc = _build(
        RawTests(unit=TierTestCounts(total=10, passed=10)),
        coverage=None,
    )
    cov_check = _check(sc, "unit", "unit.coverage")
    assert cov_check.score == 0.0
    assert cov_check.value == "n/a"
    assert sc.raw.coverage is None


def test_all_gates_always_reported() -> None:
    sc = _build(RawTests(unit=TierTestCounts(total=1, passed=1)))
    assert {g.id for g in sc.gates} == {"unit-present", "all-green", "e2e-present"}


def test_output_carries_provenance_and_raw() -> None:
    tests = RawTests(unit=TierTestCounts(total=2, passed=2))
    sc = _build(tests)
    assert sc.repo == "atlanhq/atlan-mysql-app"
    assert sc.app == "mysql"
    assert sc.commit_sha == "abc123"
    assert sc.rubric_version == "v1"
    assert sc.schema_version == "1.0"
    assert sc.generated_at == "2026-07-21T12:00:00Z"
    assert sc.raw.tests.unit.total == 2
