"""Meta-tests for the T018 integration-deselect check.

T018 fires when a ``[tool.pytest.ini_options].addopts`` ``-m 'not <marker>'``
deselection removes tests living under ``tests/integration/`` from the
directory-scoped integration CI job (``pytest tests/integration/``) — emptying
it (pytest exit 5) or silently thinning it.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.integration_deselect import (
    IntegrationTest,
    deselected_labels,
    scan_path,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ── Rule metadata ────────────────────────────────────────────────────────────


def test_t018_rule_metadata() -> None:
    rule = get_rule("T018")
    assert rule.name == "IntegrationTierDeselectedByAddopts"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.category == "test-collection"


# ── Pure core: deselected_labels ─────────────────────────────────────────────


def test_deselected_labels_matches_carriers() -> None:
    tests = [
        IntegrationTest("t.py::a", frozenset({"cloud_integration"})),
        IntegrationTest("t.py::b", frozenset({"integration"})),
        IntegrationTest("t.py::c", frozenset()),
    ]
    assert deselected_labels(tests, {"cloud_integration"}) == ["t.py::a"]


def test_deselected_labels_empty_when_no_deselection() -> None:
    tests = [IntegrationTest("t.py::a", frozenset({"cloud_integration"}))]
    assert deselected_labels(tests, set()) == []


# ── scan_text with injected integration tests ────────────────────────────────

_ALL_MARKED = [
    IntegrationTest(
        "tests/integration/test_s3.py::T::a", frozenset({"s3_integration"})
    ),
    IntegrationTest(
        "tests/integration/test_az.py::T::b", frozenset({"azure_integration"})
    ),
]


def test_fires_and_reports_exit5_when_all_deselected() -> None:
    text = (
        "[tool.pytest.ini_options]\n"
        "addopts = \"-m 'not s3_integration and not azure_integration'\"\n"
    )
    findings = scan_text(text, "pyproject.toml", integration_tests=_ALL_MARKED)
    assert [f.rule_id for f in findings] == ["T018"]
    # Whole tier deselected → message calls out the pytest exit-5 hard failure.
    assert "exit code 5" in findings[0].message


def test_fires_partial_when_some_deselected() -> None:
    text = "[tool.pytest.ini_options]\naddopts = \"-m 'not s3_integration'\"\n"
    findings = scan_text(text, "pyproject.toml", integration_tests=_ALL_MARKED)
    assert [f.rule_id for f in findings] == ["T018"]
    assert "1 of 2" in findings[0].message
    assert "exit code 5" not in findings[0].message


def test_silent_when_deselection_misses_integration_tier() -> None:
    # addopts deselects a marker no integration test carries → harmless.
    text = "[tool.pytest.ini_options]\naddopts = \"-m 'not slow'\"\n"
    assert scan_text(text, "pyproject.toml", integration_tests=_ALL_MARKED) == []


def test_silent_when_no_dash_m_deselection() -> None:
    text = '[tool.pytest.ini_options]\naddopts = "-ra -q --tb=short"\n'
    assert scan_text(text, "pyproject.toml", integration_tests=_ALL_MARKED) == []


def test_silent_when_no_addopts_at_all() -> None:
    assert scan_text('[project]\nname = "x"\n', "pyproject.toml") == []


def test_noop_without_test_context() -> None:
    # No integration_tests supplied (text-only caller): nothing to compare against.
    text = "[tool.pytest.ini_options]\naddopts = \"-m 'not s3_integration'\"\n"
    assert scan_text(text, "pyproject.toml") == []


def test_anchored_on_addopts_line() -> None:
    text = (
        "[tool.pytest.ini_options]\n"
        'asyncio_mode = "auto"\n'
        "addopts = \"-m 'not s3_integration'\"\n"
    )
    findings = scan_text(text, "pyproject.toml", integration_tests=_ALL_MARKED)
    assert findings[0].line == 3


def test_suppressed_on_addopts_line() -> None:
    text = (
        "[tool.pytest.ini_options]\n"
        "addopts = \"-m 'not s3_integration'\"  "
        "# conformance: ignore[T018] scoped to a bespoke CI job\n"
    )
    findings = scan_text(text, "pyproject.toml", integration_tests=_ALL_MARKED)
    # The finding is still produced but flagged suppressed (the runner drops it).
    assert [f.rule_id for f in findings] == ["T018"]
    assert findings[0].suppressed is True
    assert findings[0].suppression_justification == "scoped to a bespoke CI job"


# ── Filesystem end-to-end via scan_path ──────────────────────────────────────


def _write_repo(root: Path, addopts: str, test_body: str) -> None:
    (root / "pyproject.toml").write_text(
        f"[tool.pytest.ini_options]\naddopts = {addopts}\n", encoding="utf-8"
    )
    integ = root / "tests" / "integration"
    integ.mkdir(parents=True)
    (integ / "__init__.py").write_text("", encoding="utf-8")
    (integ / "test_thing.py").write_text(test_body, encoding="utf-8")


def test_scan_path_fires_on_real_repo(tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        "\"-m 'not cloud_integration'\"",
        "import pytest\n\n"
        "pytestmark = pytest.mark.cloud_integration\n\n"
        "def test_a():\n    assert True\n",
    )
    findings = scan_path(tmp_path / "pyproject.toml", tmp_path)
    assert [f.rule_id for f in findings] == ["T018"]
    assert "exit code 5" in findings[0].message


def test_scan_path_silent_on_standard_repo(tmp_path: Path) -> None:
    # Standard shape: `integration` marker, no addopts deselect (mysql/metabase).
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["integration: ..."]\n',
        encoding="utf-8",
    )
    integ = tmp_path / "tests" / "integration"
    integ.mkdir(parents=True)
    (integ / "test_thing.py").write_text(
        "import pytest\n\n"
        "pytestmark = pytest.mark.integration\n\n"
        "def test_a():\n    assert True\n",
        encoding="utf-8",
    )
    assert scan_path(tmp_path / "pyproject.toml", tmp_path) == []


def test_scan_path_noop_without_integration_dir(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = \"-m 'not integration'\"\n",
        encoding="utf-8",
    )
    assert scan_path(tmp_path / "pyproject.toml", tmp_path) == []
