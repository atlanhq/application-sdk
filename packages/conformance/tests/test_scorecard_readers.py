"""Tests for the scorecard evidence parsers (junit + coverage.py JSON)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.scorecard.readers import (
    parse_coverage_json,
    parse_junit,
    parse_junit_tier,
    parse_junit_tier_merged,
    resolve_junit_paths,
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


# ---------------------------------------------------------------------------
# Per-leg e2e evidence (FND-33): N junits, one per suite x cloud
# ---------------------------------------------------------------------------


def _leg(tmp_path: Path, name: str, *cases: tuple[str, str, float]) -> Path:
    """Write a one-suite junit; each case is ``(test_name, outcome, time)``."""
    child = {
        "passed": "",
        "failed": "<failure/>",
        "errors": "<error/>",
        "skipped": "<skipped/>",
    }
    body = "".join(
        f'<testcase classname="tests.e2e.test_s" name="{case}" time="{t}">'
        f"{child[outcome]}</testcase>"
        for case, outcome, t in cases
    )
    path = tmp_path / f"{name}.xml"
    path.write_text(
        f'<testsuites><testsuite name="pytest" tests="{len(cases)}">{body}'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def test_merged_legs_dedupe_the_same_test_across_clouds(tmp_path: Path) -> None:
    """Three clouds x two tests is two tests, not six.

    Summing would make the denominator a function of how many clouds a repo has
    onboarded, so onboarding a cloud would raise the pass rate by diluting an
    existing failure.
    """
    legs = [
        _leg(tmp_path, cloud, ("t_a", "passed", 1.0), ("t_b", "passed", 2.0))
        for cloud in ("aws", "azure", "gcp")
    ]
    counts = parse_junit_tier_merged(legs)
    assert counts.total == 2
    assert counts.passed == 2
    assert counts.ran == 2
    assert counts.green is True


def test_merged_legs_take_the_worst_outcome_per_test(tmp_path: Path) -> None:
    """A test failing on ONE cloud is not passing."""
    counts = parse_junit_tier_merged(
        [
            _leg(tmp_path, "aws", ("t_a", "passed", 1.0), ("t_b", "passed", 1.0)),
            _leg(tmp_path, "azure", ("t_a", "passed", 1.0), ("t_b", "failed", 1.0)),
        ]
    )
    assert counts.total == 2
    assert counts.passed == 1
    assert counts.failed == 1
    assert counts.green is False


def test_merged_legs_rank_error_above_failure(tmp_path: Path) -> None:
    counts = parse_junit_tier_merged(
        [
            _leg(tmp_path, "aws", ("t_a", "failed", 1.0)),
            _leg(tmp_path, "azure", ("t_a", "errors", 1.0)),
        ]
    )
    assert counts.errors == 1
    assert counts.failed == 0


def test_merged_legs_prefer_a_real_run_over_a_skip(tmp_path: Path) -> None:
    """A pass on one cloud and a self-skip on another is a test that RAN.

    Ranking skip highest would erase the evidence a leg actually produced and
    push the tier back to ``present: false``.
    """
    counts = parse_junit_tier_merged(
        [
            _leg(tmp_path, "aws", ("t_a", "passed", 1.0)),
            _leg(tmp_path, "azure", ("t_a", "skipped", 0.0)),
        ]
    )
    assert counts.passed == 1
    assert counts.skipped == 0
    assert counts.ran == 1
    assert counts.present is True


def test_merged_legs_take_the_max_duration_per_test(tmp_path: Path) -> None:
    counts = parse_junit_tier_merged(
        [
            _leg(tmp_path, "aws", ("t_a", "passed", 1.5)),
            _leg(tmp_path, "azure", ("t_a", "passed", 4.0)),
        ]
    )
    assert counts.duration_sec == pytest.approx(4.0)


def test_merged_with_no_paths_is_empty_not_an_error() -> None:
    counts = parse_junit_tier_merged([])
    assert counts.total == 0
    assert counts.present is False


def test_resolve_junit_paths_expands_globs_and_dedupes(tmp_path: Path) -> None:
    for cloud in ("aws", "azure"):
        leg_dir = tmp_path / f"suite-{cloud}" / "results"
        leg_dir.mkdir(parents=True)
        (leg_dir / "sdr-test-results.xml").write_text("<testsuites/>", encoding="utf-8")
    pattern = str(tmp_path / "*" / "results" / "sdr-test-results.xml")
    # The same file reached by a glob and by its literal path resolves once.
    resolved = resolve_junit_paths(
        [pattern, str(tmp_path / "suite-aws" / "results" / "sdr-test-results.xml")]
    )
    assert len(resolved) == 2


def test_resolve_junit_paths_matching_nothing_is_empty(tmp_path: Path) -> None:
    """Absent evidence must be distinguishable from zero-scored evidence."""
    assert resolve_junit_paths([str(tmp_path / "*" / "nope.xml")]) == []
    assert resolve_junit_paths([""]) == []


def test_resolve_junit_paths_skips_a_directory_match(tmp_path: Path) -> None:
    """A glob can land on a directory named *.xml — never hand that to ET.parse."""
    (tmp_path / "results.xml").mkdir()
    (tmp_path / "real.xml").write_text("<testsuites/>", encoding="utf-8")
    resolved = resolve_junit_paths([str(tmp_path / "*.xml")])
    assert resolved == [str(tmp_path / "real.xml")]


def test_resolve_junit_paths_skips_a_non_junit_xml(tmp_path: Path) -> None:
    """An unrelated XML must not fold its <testcase> elements into e2e counts."""
    (tmp_path / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion></project>", encoding="utf-8"
    )
    (tmp_path / "results.xml").write_text("<testsuites/>", encoding="utf-8")
    resolved = resolve_junit_paths([str(tmp_path / "*.xml")])
    assert resolved == [str(tmp_path / "results.xml")]


def test_resolve_junit_paths_skips_a_malformed_xml(tmp_path: Path) -> None:
    """A truncated / binary-ish .xml must not crash the scorecard."""
    (tmp_path / "broken.xml").write_bytes(b"\x00\x01not xml")
    (tmp_path / "real.xml").write_text(
        '<testsuite name="pytest" tests="1"/>', encoding="utf-8"
    )
    resolved = resolve_junit_paths([str(tmp_path / "*.xml")])
    assert resolved == [str(tmp_path / "real.xml")]


def test_resolve_junit_paths_accepts_a_testsuite_root(tmp_path: Path) -> None:
    """Both junit root forms (<testsuites> and bare <testsuite>) are accepted."""
    (tmp_path / "suites.xml").write_text("<testsuites/>", encoding="utf-8")
    (tmp_path / "single.xml").write_text(
        '<testsuite name="pytest" tests="0"/>', encoding="utf-8"
    )
    resolved = resolve_junit_paths([str(tmp_path / "*.xml")])
    assert resolved == [str(tmp_path / "single.xml"), str(tmp_path / "suites.xml")]


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
