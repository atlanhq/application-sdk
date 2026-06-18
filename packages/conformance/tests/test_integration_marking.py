"""Meta-tests for the T-series test-marking check (T001).

This check is shipped in the conformance package and fanned out across the fleet
— a buggy check false-positives across hundreds of apps and triggers spurious
remediations.  So T001 is tested to fire *exactly* when a test under
``tests/integration/`` carries no marker that deselects it from the unit job, and
to stay silent for every valid way of applying one (module ``pytestmark``, class
decorator, function decorator) — including repo-specific markers like
``s3_integration`` derived from ``pyproject.toml`` ``addopts`` — as well as for
non-test code and inline suppressions.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.integration_marking import (
    _deselected_markers,
    accepted_markers_for_repo,
    discover,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_FILE = "tests/integration/test_x.py"


def _ids(src: str) -> list[str]:
    return [f.rule_id for f in scan_text(src, _FILE)]


def _active(src: str) -> list[str]:
    """Rule IDs for findings that are NOT suppressed."""
    return [f.rule_id for f in scan_text(src, _FILE) if not f.suppressed]


# ── T001 fires on unmarked tests ────────────────────────────────────────────


def test_t001_fires_on_unmarked_function() -> None:
    src = "import pytest\n\n\ndef test_alpha():\n    pass\n"
    assert _ids(src) == ["T001"]


def test_t001_fires_on_unmarked_method_in_test_class() -> None:
    src = (
        "import pytest\n\n\nclass TestThing:\n    def test_beta(self):\n        pass\n"
    )
    assert _ids(src) == ["T001"]


def test_t001_one_finding_per_unmarked_item() -> None:
    src = (
        "import pytest\n\n\n"
        "def test_alpha():\n    pass\n\n\n"
        "class TestThing:\n"
        "    def test_beta(self):\n        pass\n"
        "    def test_gamma(self):\n        pass\n"
    )
    assert _ids(src) == ["T001", "T001", "T001"]


def test_t001_finding_points_at_def_line() -> None:
    src = "import pytest\n\n\ndef test_alpha():\n    pass\n"
    [finding] = scan_text(src, _FILE)
    assert finding.line == 4  # the `def` line
    assert "test_alpha" in finding.message


# ── T001 silent on every valid marking form ─────────────────────────────────


def test_t001_silent_on_module_pytestmark_bare() -> None:
    src = "import pytest\n\npytestmark = pytest.mark.integration\n\n\ndef test_a():\n    pass\n"
    assert _ids(src) == []


def test_t001_silent_on_module_pytestmark_list() -> None:
    src = (
        "import pytest\n\n"
        "pytestmark = [pytest.mark.integration]\n\n\n"
        "def test_a():\n    pass\n"
    )
    assert _ids(src) == []


def test_t001_silent_on_module_pytestmark_among_others() -> None:
    src = (
        "import pytest\n\n"
        "pytestmark = [pytest.mark.asyncio, pytest.mark.integration]\n\n\n"
        "def test_a():\n    pass\n"
    )
    assert _ids(src) == []


def test_t001_silent_on_function_decorator() -> None:
    src = "import pytest\n\n\n@pytest.mark.integration\ndef test_a():\n    pass\n"
    assert _ids(src) == []


def test_t001_silent_on_called_decorator_form() -> None:
    src = "import pytest\n\n\n@pytest.mark.integration()\ndef test_a():\n    pass\n"
    assert _ids(src) == []


def test_t001_silent_on_class_decorator_covers_methods() -> None:
    src = (
        "import pytest\n\n\n"
        "@pytest.mark.integration\n"
        "class TestX:\n"
        "    def test_a(self):\n        pass\n"
        "    def test_b(self):\n        pass\n"
    )
    assert _ids(src) == []


def test_t001_silent_on_per_method_decorator() -> None:
    src = (
        "import pytest\n\n\n"
        "class TestX:\n"
        "    @pytest.mark.integration\n"
        "    def test_a(self):\n        pass\n"
    )
    assert _ids(src) == []


def test_t001_silent_on_from_pytest_import_mark() -> None:
    src = "from pytest import mark\n\npytestmark = mark.integration\n\n\ndef test_a():\n    pass\n"
    assert _ids(src) == []


def test_t001_silent_on_aliased_pytest_module() -> None:
    src = (
        "import pytest as pt\n\n"
        "pytestmark = pt.mark.integration\n\n\n"
        "def test_a():\n    pass\n"
    )
    assert _ids(src) == []


# ── T001 mirrors pytest collection (no false positives) ─────────────────────


def test_t001_ignores_non_test_functions() -> None:
    src = "import pytest\n\n\ndef helper():\n    pass\n"
    assert _ids(src) == []


def test_t001_ignores_non_test_classes() -> None:
    # Helper classes (e.g. App/Input/Output subclasses) are never collected.
    src = (
        "import pytest\n\n\n"
        "class Helper:\n"
        "    def test_looks_like_a_test(self):\n        pass\n"
    )
    assert _ids(src) == []


def test_t001_only_flags_unmarked_methods_in_partially_marked_class() -> None:
    src = (
        "import pytest\n\n\n"
        "class TestX:\n"
        "    @pytest.mark.integration\n"
        "    def test_marked(self):\n        pass\n"
        "    def test_unmarked(self):\n        pass\n"
    )
    findings = scan_text(src, _FILE)
    assert [f.rule_id for f in findings] == ["T001"]
    assert "test_unmarked" in findings[0].message


def test_t001_silent_on_syntax_error() -> None:
    assert _ids("def test_a(:\n") == []


# ── Inline suppression ──────────────────────────────────────────────────────


def test_t001_suppressed_inline_same_line() -> None:
    src = (
        "import pytest\n\n\n"
        "def test_a():  # conformance: ignore[T001] dynamic marker via add_marker\n"
        "    pass\n"
    )
    findings = scan_text(src, _FILE)
    assert [f.rule_id for f in findings] == ["T001"]
    assert findings[0].suppressed is True
    assert _active(src) == []


def test_t001_suppressed_comment_line_above() -> None:
    src = (
        "import pytest\n\n\n"
        "# conformance: ignore[T001] dynamic marker\n"
        "def test_a():\n    pass\n"
    )
    assert _active(src) == []


# ── discover() targets tests/integration only ───────────────────────────────


def test_discover_finds_integration_test_files(tmp_path: Path) -> None:
    integ = tmp_path / "tests" / "integration"
    integ.mkdir(parents=True)
    (integ / "test_one.py").write_text("def test_a():\n    pass\n")
    (integ / "two_test.py").write_text("def test_b():\n    pass\n")
    (integ / "conftest.py").write_text("# not a test file\n")
    (integ / "helpers.py").write_text("# not a test file\n")
    # Files outside tests/integration/ are not discovered by the T-series.
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_unit.py").write_text(
        "def test_c():\n    pass\n"
    )

    found = {p.name for p in discover(tmp_path)}
    assert found == {"test_one.py", "two_test.py"}


def test_discover_empty_when_dir_absent(tmp_path: Path) -> None:
    assert discover(tmp_path) == []


# ── Accepted-marker set (repo-derived, not hard-coded to `integration`) ──────


def test_t001_silent_on_accepted_repo_specific_marker() -> None:
    # A repo whose unit job deselects s3_integration accepts that marker too.
    src = "import pytest\n\n\n@pytest.mark.s3_integration\ndef test_a():\n    pass\n"
    accepted = {"integration", "s3_integration"}
    assert [f.rule_id for f in scan_text(src, _FILE, accepted_markers=accepted)] == []


def test_t001_silent_on_module_pytestmark_repo_specific_marker() -> None:
    src = (
        "import pytest\n\n"
        "pytestmark = pytest.mark.storage_emulator\n\n\n"
        "def test_a():\n    pass\n"
    )
    accepted = {"integration", "storage_emulator"}
    assert [f.rule_id for f in scan_text(src, _FILE, accepted_markers=accepted)] == []


def test_t001_fires_on_non_deselecting_marker_only() -> None:
    # asyncio is a real marker but does NOT keep the test out of the unit job.
    src = "import pytest\n\n\n@pytest.mark.asyncio\ndef test_a():\n    pass\n"
    assert _ids(src) == ["T001"]


def test_t001_default_accepted_is_integration_only() -> None:
    # With no accepted set provided, s3_integration alone is not enough.
    src = "import pytest\n\n\n@pytest.mark.s3_integration\ndef test_a():\n    pass\n"
    assert _ids(src) == ["T001"]


# ── addopts -> accepted-marker derivation ────────────────────────────────────


def test_deselected_markers_from_string_addopts() -> None:
    addopts = "-m 'not integration and not e2e and not s3_integration'"
    assert _deselected_markers(addopts) == {"integration", "e2e", "s3_integration"}


def test_deselected_markers_from_list_addopts() -> None:
    addopts = ["-v", "-m", "not integration and not storage_emulator"]
    assert _deselected_markers(addopts) == {"integration", "storage_emulator"}


def test_deselected_markers_empty_without_dash_m() -> None:
    assert _deselected_markers("-v --tb=short") == frozenset()
    assert _deselected_markers(None) == frozenset()


def test_accepted_markers_for_repo_reads_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "addopts = \"-m 'not integration and not s3_integration'\"\n"
    )
    assert accepted_markers_for_repo(tmp_path) == {"integration", "s3_integration"}


def test_accepted_markers_for_repo_falls_back_when_absent(tmp_path: Path) -> None:
    # No pyproject.toml at all → default {"integration"}.
    assert accepted_markers_for_repo(tmp_path) == {"integration"}
    # pyproject without an -m addopts → also the default.
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert accepted_markers_for_repo(tmp_path) == {"integration"}


# ── Rule definition wiring ──────────────────────────────────────────────────


def test_t001_rule_metadata() -> None:
    rule = get_rule("T001")
    assert rule.name == "UnmarkedIntegrationTest"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.BOTH  # useful on the SDK too, not app-only
    assert rule.autofixable is False  # detect-only → residue (loop can't edit tests/)
    assert rule.rationale.strip()
