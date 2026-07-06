"""Meta-tests for the T010–T013 test-tier structure/placement checks (BLDX-1400).

T010–T012 check that ``tests/{unit,integration,e2e}/`` each contain at least
one collectable pytest test; T010 (unit) is never exemptable, T011/T012 are
exemptable per-repo via ``[tool.conformance].exempt_test_tiers`` in
``pyproject.toml`` (since ``atlan.yaml`` is generated and must not be
hand-edited). T013 flags a collectable test file placed outside all four
canonical tier directories.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.test_structure import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_ONE_TEST = "def test_a():\n    assert 1 == 1\n"


def _write(tmp_path: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _run(tmp_path: Path) -> list:
    paths = discover(tmp_path)
    return scan_all(paths, tmp_path)


def _ids(findings: list) -> list[str]:
    return sorted(f.rule_id for f in findings)


# ── Rule metadata ────────────────────────────────────────────────────────────


def test_t010_rule_metadata() -> None:
    rule = get_rule("T010")
    assert rule.name == "MissingUnitTestSuite"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.category == "test-tier-coverage"


def test_t011_rule_metadata() -> None:
    rule = get_rule("T011")
    assert rule.name == "MissingIntegrationTestSuite"
    assert rule.scope == RuleScope.APP


def test_t012_rule_metadata() -> None:
    rule = get_rule("T012")
    assert rule.name == "MissingE2ETestSuite"
    assert rule.scope == RuleScope.APP


def test_t013_rule_metadata() -> None:
    rule = get_rule("T013")
    assert rule.name == "TestFileOutsideTierDir"
    assert rule.scope == RuleScope.BOTH


# ── T010/T011/T012: missing tier suites ──────────────────────────────────────


def test_all_three_tiers_present_is_clean(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n',
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/integration/test_bar.py": _ONE_TEST,
            "tests/e2e/test_baz.py": _ONE_TEST,
        },
    )
    assert _run(tmp_path) == []


def test_e2e_subclass_with_no_own_test_methods_counts_as_present(
    tmp_path: Path,
) -> None:
    """Regression: the canonical e2e pattern inherits every test* method from a
    generated base class and defines none of its own — that must still count
    as "has tests" for T012, not a false positive missing-suite finding.
    """
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n',
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/integration/test_bar.py": _ONE_TEST,
            "tests/e2e/test_foo_e2e.py": (
                "from app.generated._e2e_base import FooGeneratedE2EBase\n\n\n"
                "class TestFooE2E(FooGeneratedE2EBase):\n"
                "    expected_min_asset_counts = {'Table': 1}\n"
            ),
        },
    )
    assert _run(tmp_path) == []


def test_bare_test_class_with_no_bases_and_no_methods_is_not_counted(
    tmp_path: Path,
) -> None:
    """The one shape this still misses: a `class Test*:` with no base classes
    and no own test methods — pytest collects it but finds zero tests, so
    "missing" is actually correct here.
    """
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n',
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/integration/test_bar.py": _ONE_TEST,
            "tests/e2e/test_empty.py": "class TestEmpty:\n    pass\n",
        },
    )
    assert _ids(_run(tmp_path)) == ["T012"]


def test_missing_all_tiers_fires_t010_t011_t012(tmp_path: Path) -> None:
    _write(tmp_path, {"pyproject.toml": '[project]\nname = "x"\n'})
    assert _ids(_run(tmp_path)) == ["T010", "T011", "T012"]


def test_no_tests_dir_at_all_fires_t010_t011_t012(tmp_path: Path) -> None:
    _write(tmp_path, {"pyproject.toml": '[project]\nname = "x"\n'})
    # discover() returns [] when tests/ is absent entirely.
    assert discover(tmp_path) == []
    assert _ids(scan_all([], tmp_path)) == ["T010", "T011", "T012"]


def test_t010_not_exemptable(tmp_path: Path) -> None:
    """Unlike T011/T012, exempt_test_tiers never suppresses T010."""
    _write(
        tmp_path,
        {
            "pyproject.toml": (
                "[tool.conformance]\n"
                'exempt_test_tiers = ["unit", "integration", "e2e"]\n'
            ),
        },
    )
    assert "T010" in _ids(_run(tmp_path))


def test_t011_t012_exempted_via_pyproject_config(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": (
                '[tool.conformance]\nexempt_test_tiers = ["integration", "e2e"]\n'
            ),
            "tests/unit/test_foo.py": _ONE_TEST,
        },
    )
    assert _run(tmp_path) == []


def test_partial_exemption_still_fires_for_non_exempt_tier(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[tool.conformance]\nexempt_test_tiers = ["e2e"]\n',
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/integration/test_bar.py": _ONE_TEST,
        },
    )
    assert _run(tmp_path) == []


def test_tier_dir_present_but_empty_still_fires(tmp_path: Path) -> None:
    """A tests/integration/ dir with no collectable test is still 'missing'."""
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n',
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/integration/__init__.py": "",
            "tests/e2e/test_baz.py": _ONE_TEST,
        },
    )
    assert _ids(_run(tmp_path)) == ["T011"]


def test_t010_t011_t012_anchored_on_pyproject_toml(tmp_path: Path) -> None:
    _write(tmp_path, {"pyproject.toml": '[project]\nname = "x"\n'})
    findings = _run(tmp_path)
    for f in findings:
        assert f.file == "pyproject.toml"
        assert f.line == 1


def test_t011_suppressed_via_first_line_directive(tmp_path: Path) -> None:
    """A suppressed finding is still reported (for audit) but marked suppressed."""
    _write(
        tmp_path,
        {
            "pyproject.toml": (
                "# conformance: ignore[T011] adoption in progress, tracked in BLDX-9999\n"
                '[project]\nname = "x"\n'
            ),
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/e2e/test_baz.py": _ONE_TEST,
        },
    )
    findings = _run(tmp_path)
    assert [f.rule_id for f in findings] == ["T011"]
    assert findings[0].suppressed is True


# ── T013: test file outside tier dirs ────────────────────────────────────────


def test_t013_fires_on_loose_file_directly_under_tests(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n',
            "tests/test_loose.py": _ONE_TEST,
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/integration/test_bar.py": _ONE_TEST,
            "tests/e2e/test_baz.py": _ONE_TEST,
        },
    )
    findings = _run(tmp_path)
    t013 = [f for f in findings if f.rule_id == "T013"]
    assert len(t013) == 1
    assert t013[0].file == "tests/test_loose.py"


def test_t013_fires_on_non_canonical_subdirectory(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n',
            "tests/scratch/test_wip.py": _ONE_TEST,
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/integration/test_bar.py": _ONE_TEST,
            "tests/e2e/test_baz.py": _ONE_TEST,
        },
    )
    findings = _run(tmp_path)
    assert [f.rule_id for f in findings] == ["T013"]


def test_t013_silent_on_ui_tier(tmp_path: Path) -> None:
    """tests/ui/ is a recognised placement even though it has no missing-suite rule."""
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n',
            "tests/ui/test_playwright.py": _ONE_TEST,
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/integration/test_bar.py": _ONE_TEST,
            "tests/e2e/test_baz.py": _ONE_TEST,
        },
    )
    assert not any(f.rule_id == "T013" for f in _run(tmp_path))


def test_t013_silent_when_no_tests_outside_tier_dirs(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n',
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/integration/test_bar.py": _ONE_TEST,
            "tests/e2e/test_baz.py": _ONE_TEST,
        },
    )
    assert not any(f.rule_id == "T013" for f in _run(tmp_path))


def test_t013_silent_on_non_collectable_loose_file(tmp_path: Path) -> None:
    """A loose helper module (not test_*.py) isn't a T013 concern."""
    _write(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n',
            "tests/helpers.py": "def build_fixture():\n    return {}\n",
            "tests/unit/test_foo.py": _ONE_TEST,
            "tests/integration/test_bar.py": _ONE_TEST,
            "tests/e2e/test_baz.py": _ONE_TEST,
        },
    )
    assert not any(f.rule_id == "T013" for f in _run(tmp_path))
