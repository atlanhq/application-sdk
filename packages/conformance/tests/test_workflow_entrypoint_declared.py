"""Tests for T020 UndeclaredWorkflowEntrypoint."""

from __future__ import annotations

import textwrap
from pathlib import Path

from conformance.suite.checks.workflow_entrypoint_declared import (
    app_entrypoint_count,
    discover,
    scan_path,
    scan_text,
)

_MULTI = 2


def _scan(source: str, count: int = _MULTI):
    return scan_text(
        textwrap.dedent(source), "tests/integration/test_x.py", entrypoint_count=count
    )


# ---------------------------------------------------------------- app side


def _write_app(tmp_path: Path, source: str) -> Path:
    app = tmp_path / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "main.py").write_text(textwrap.dedent(source))
    return tmp_path


def test_counts_distinct_entrypoints(tmp_path):
    repo = _write_app(
        tmp_path,
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, i): ...

            @entrypoint(name="miner")
            async def miner(self, i): ...

            @task
            async def fetch(self, i): ...
        """,
    )
    assert app_entrypoint_count(repo) == 2


def test_no_app_package_counts_zero(tmp_path):
    assert app_entrypoint_count(tmp_path) == 0


# ------------------------------------------------------------- rule scope


def test_single_entrypoint_app_is_never_flagged():
    """Declaring is noise when there is only one possible target."""
    findings = _scan(
        """
        Scenario(name="run", api="workflow", assert_that={})
        """,
        count=1,
    )
    assert findings == []


def test_non_workflow_scenarios_are_ignored():
    findings = _scan(
        """
        Scenario(name="auth", api="auth", assert_that={})
        Scenario(name="pre", api="preflight", assert_that={})
        """
    )
    assert findings == []


# ------------------------------------------------------------- detection


def test_undeclared_workflow_scenario_is_flagged():
    findings = _scan(
        """
        Scenario(name="crawl", api="workflow", assert_that={})
        """
    )
    assert len(findings) == 1
    assert findings[0].rule_id == "T020"
    assert "crawl" in findings[0].message
    assert "declares 2" in findings[0].message


def test_declared_entrypoint_satisfies_the_rule():
    findings = _scan(
        """
        Scenario(name="crawl", api="workflow", assert_that={}, entrypoint="crawler")
        """
    )
    assert findings == []


def test_explicit_endpoint_override_satisfies_the_rule():
    """An explicit endpoint is already unambiguous."""
    findings = _scan(
        """
        Scenario(name="crawl", api="workflow", assert_that={},
                 endpoint="/start?entrypoint=miner")
        """
    )
    assert findings == []


def test_class_level_entrypoint_covers_its_scenarios():
    findings = _scan(
        """
        class TestSuite(BaseIntegrationTest):
            entrypoint = "crawler"
            scenarios = [
                Scenario(name="crawl", api="workflow", assert_that={}),
            ]
        """
    )
    assert findings == []


def test_class_without_entrypoint_does_not_cover_its_scenarios():
    findings = _scan(
        """
        class TestSuite(BaseIntegrationTest):
            timeout = 30
            scenarios = [
                Scenario(name="crawl", api="workflow", assert_that={}),
            ]
        """
    )
    assert len(findings) == 1


def test_each_undeclared_scenario_is_reported_separately():
    findings = _scan(
        """
        Scenario(name="a", api="workflow", assert_that={})
        Scenario(name="b", api="workflow", assert_that={}, entrypoint="miner")
        Scenario(name="c", api="workflow", assert_that={})
        """
    )
    assert [f.message.split("'")[1] for f in findings] == ["a", "c"]


def test_inline_suppression_is_honoured():
    findings = _scan(
        """
        # conformance: ignore[T020] single-workflow suite, tracked in X-123
        Scenario(name="crawl", api="workflow", assert_that={})
        """
    )
    assert all(f.suppressed for f in findings)


def test_unparseable_file_is_skipped():
    assert scan_text("def broken(:\n", "tests/x.py", entrypoint_count=_MULTI) == []


# -------------------------------------------------------------- discovery


def test_discover_walks_every_test_tier(tmp_path):
    """Scenario suites live under e2e/ and sdr/ too, not just integration/."""
    for rel in [
        "tests/integration/test_a.py",
        "tests/e2e/test_b.py",
        "tests/sdr/test_c.py",
        "tests/unit/helpers.py",
    ]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    found = {p.name for p in discover(tmp_path)}
    assert found == {"test_a.py", "test_b.py", "test_c.py"}


def test_discover_returns_empty_without_tests(tmp_path):
    assert discover(tmp_path) == []


def test_scan_path_resolves_the_apps_entrypoint_count(tmp_path):
    repo = _write_app(
        tmp_path,
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, i): ...

            @entrypoint
            async def miner(self, i): ...
        """,
    )
    test = repo / "tests" / "integration" / "test_x.py"
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text('Scenario(name="crawl", api="workflow", assert_that={})\n')
    findings = scan_path(test, repo)
    assert len(findings) == 1
    assert findings[0].file == "tests/integration/test_x.py"
