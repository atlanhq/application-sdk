"""Tests for the scorecard evidence parsers (junit + coverage.py JSON)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.scorecard.readers import (
    parse_coverage_json,
    parse_junit,
    parse_junit_tier,
    tier_for_path,
)
from conformance.scorecard.schema import CoverageMetrics, RawTests, TierTestCounts

_FIXTURES = Path(__file__).parent / "fixtures" / "scorecard"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/unit/test_a.py", "unit"),
        ("tests/integration/test_b.py", "integration"),
        ("tests/e2e/test_c.py", "e2e"),
        # dotted classname form
        ("tests.e2e.test_c", "e2e"),
        ("tests.integration.test_b", "integration"),
        # windows separators
        ("tests\\e2e\\test_c.py", "e2e"),
        # substring traps must NOT match — only whole segments
        ("tests/reintegration/test_d.py", "unit"),
        ("tests/e2every/test_x.py", "unit"),
        # unclassified / bare paths fall back to unit (the catch-all base)
        ("tests/test_top.py", "unit"),
        ("", "unit"),
    ],
)
def test_tier_for_path(path: str, expected: str) -> None:
    assert tier_for_path(path) == expected


def test_parse_junit_buckets_and_counts() -> None:
    tests = parse_junit(_FIXTURES / "junit_mixed.xml")
    assert isinstance(tests, RawTests)

    # unit: 3 pass + 1 fail
    assert tests.unit.total == 4
    assert tests.unit.passed == 3
    assert tests.unit.failed == 1
    assert tests.unit.skipped == 0
    assert tests.unit.errors == 0
    assert tests.unit.ran == 4
    assert tests.unit.present is True
    assert tests.unit.green is False
    assert tests.unit.duration_sec == pytest.approx(0.65)

    # integration: 2 pass
    assert tests.integration.total == 2
    assert tests.integration.passed == 2
    assert tests.integration.green is True
    assert tests.integration.duration_sec == pytest.approx(3.0)

    # e2e: 1 skipped + 1 error → ran == 1, not present-as-green
    assert tests.e2e.total == 2
    assert tests.e2e.skipped == 1
    assert tests.e2e.errors == 1
    assert tests.e2e.passed == 0
    assert tests.e2e.ran == 1
    assert tests.e2e.green is False


def test_parse_junit_tier_attributes_all_testcases_to_one_tier() -> None:
    """The per-tier-file reader ignores paths and sums every testcase."""
    counts = parse_junit_tier(_FIXTURES / "junit_mixed.xml")
    assert isinstance(counts, TierTestCounts)
    # whole file (across all dirs): 8 total = 5 pass + 1 fail + 1 skip + 1 error
    assert counts.total == 8
    assert counts.passed == 5
    assert counts.failed == 1
    assert counts.skipped == 1
    assert counts.errors == 1


def test_parse_junit_classname_fallback_when_no_file_attr(tmp_path: Path) -> None:
    xml = tmp_path / "j.xml"
    xml.write_text(
        '<testsuites><testsuite name="pytest" tests="1">'
        '<testcase classname="tests.e2e.test_z" name="t" time="0.1"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    tests = parse_junit(xml)
    assert tests.e2e.total == 1
    assert tests.e2e.passed == 1
    assert tests.unit.total == 0


def test_parse_junit_accepts_testsuite_root(tmp_path: Path) -> None:
    """Some emitters use <testsuite> as the root rather than <testsuites>."""
    xml = tmp_path / "j.xml"
    xml.write_text(
        '<testsuite name="pytest" tests="1">'
        '<testcase classname="tests.unit.test_z" name="t" '
        'file="tests/unit/test_z.py" time="0.5"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    tests = parse_junit(xml)
    assert tests.unit.total == 1
    assert tests.unit.passed == 1


def test_parse_coverage_json_totals_and_branch() -> None:
    cov = parse_coverage_json(_FIXTURES / "coverage.json")
    assert isinstance(cov, CoverageMetrics)
    assert cov.lines_covered == 86
    assert cov.lines_valid == 100
    assert cov.percent == pytest.approx(86.0)
    # 31 / 40 * 100
    assert cov.branch_percent == pytest.approx(77.5)


def test_parse_coverage_json_no_branches_leaves_branch_none(tmp_path: Path) -> None:
    cov_file = tmp_path / "coverage.json"
    cov_file.write_text(
        '{"totals": {"covered_lines": 5, "num_statements": 10, '
        '"percent_covered": 50.0, "num_branches": 0, "covered_branches": 0}}',
        encoding="utf-8",
    )
    cov = parse_coverage_json(cov_file)
    assert cov.percent == pytest.approx(50.0)
    assert cov.branch_percent is None
