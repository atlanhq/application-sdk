"""Meta-tests for the T019 asyncio-loop-scope check.

T019 fires when ``[tool.pytest.ini_options].asyncio_default_fixture_loop_scope``
is set to a broadened scope (``session`` / ``package`` / ``module`` / ``class``)
while ``asyncio_default_test_loop_scope`` is unset (defaults to ``function``)
**and** a collectable test drives workflow execution from its body (an awaited
``execute_app`` / ``execute_workflow`` / ``start_workflow``). Like T018 it is
correlated with the real suite — the config mismatch alone does not fire.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks.asyncio_loop_scope import (
    _broadened_fixture_scope,
    _drives_workflow_in_body,
    in_body_workflow_drivers,
    scan_path,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_RISKY_CFG = (
    "[tool.pytest.ini_options]\n"
    'asyncio_mode = "auto"\n'
    'asyncio_default_fixture_loop_scope = "session"\n'
)
_DRIVER = ["tests/integration/test_x.py::test_drives"]


# ── Rule metadata ────────────────────────────────────────────────────────────


def test_t019_rule_metadata() -> None:
    rule = get_rule("T019")
    assert rule.name == "AsyncioTestLoopScopeUnset"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.BOTH
    assert rule.category == "test-async-config"


# ── Config detection: _broadened_fixture_scope ───────────────────────────────


def test_broadened_scope_detected_for_each_nonfunction_scope() -> None:
    for scope in ("session", "package", "module", "class"):
        text = (
            "[tool.pytest.ini_options]\n"
            f'asyncio_default_fixture_loop_scope = "{scope}"\n'
        )
        assert _broadened_fixture_scope(text) == scope


def test_not_risky_when_test_scope_set_explicitly() -> None:
    text = (
        "[tool.pytest.ini_options]\n"
        'asyncio_default_fixture_loop_scope = "session"\n'
        'asyncio_default_test_loop_scope = "session"\n'
    )
    assert _broadened_fixture_scope(text) is None


def test_not_risky_when_test_scope_set_to_function() -> None:
    text = (
        "[tool.pytest.ini_options]\n"
        'asyncio_default_fixture_loop_scope = "session"\n'
        'asyncio_default_test_loop_scope = "function"\n'
    )
    assert _broadened_fixture_scope(text) is None


def test_not_risky_when_fixture_scope_function_or_unset() -> None:
    assert (
        _broadened_fixture_scope(
            '[tool.pytest.ini_options]\nasyncio_default_fixture_loop_scope = "function"\n'
        )
        is None
    )
    assert (
        _broadened_fixture_scope('[tool.pytest.ini_options]\nasyncio_mode = "auto"\n')
        is None
    )


def test_not_risky_on_invalid_scope_or_malformed_toml() -> None:
    # A typo pytest-asyncio would itself reject — not this rule's concern.
    assert (
        _broadened_fixture_scope(
            '[tool.pytest.ini_options]\nasyncio_default_fixture_loop_scope = "sesion"\n'
        )
        is None
    )
    assert _broadened_fixture_scope("[tool.pytest.ini_options\n = = =") is None


# ── In-body driver detection ─────────────────────────────────────────────────


def _fn(src: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    node = ast.parse(src).body[0]
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    return node


def test_driver_detected_for_each_submission_name() -> None:
    for call in ("execute_app", "execute_workflow", "start_workflow"):
        fn = _fn(
            f"async def test_x(ex):\n    r = await ex.{call}(App, inp)\n    assert r\n"
        )
        assert _drives_workflow_in_body(fn) is True, call


def test_driver_detected_for_bare_name_call() -> None:
    fn = _fn("async def test_x():\n    r = await execute_workflow(w)\n    assert r\n")
    assert _drives_workflow_in_body(fn) is True


def test_no_driver_when_awaiting_unrelated_call() -> None:
    # httpx/DB reads etc. that are not the workflow-submission surface.
    fn = _fn("async def test_x(c):\n    r = await c.load(creds)\n    assert r\n")
    assert _drives_workflow_in_body(fn) is False


def test_no_driver_when_call_not_awaited() -> None:
    fn = _fn("def test_x(ex):\n    ex.execute_app(App, inp)\n")
    assert _drives_workflow_in_body(fn) is False


# ── scan_text: correlation of config + drivers ───────────────────────────────


def test_fires_when_config_risky_and_driver_present() -> None:
    findings = scan_text(_RISKY_CFG, "pyproject.toml", in_body_drivers=_DRIVER)
    assert [f.rule_id for f in findings] == ["T019"]
    assert "asyncio_default_test_loop_scope" in findings[0].message
    assert "test_drives" in findings[0].message


def test_silent_when_config_risky_but_no_driver() -> None:
    # CONFIG-ONLY: mismatched config, but all execution lives in fixtures.
    assert scan_text(_RISKY_CFG, "pyproject.toml", in_body_drivers=[]) == []


def test_silent_when_driver_present_but_config_safe() -> None:
    safe = (
        "[tool.pytest.ini_options]\n"
        'asyncio_default_fixture_loop_scope = "session"\n'
        'asyncio_default_test_loop_scope = "session"\n'
    )
    assert scan_text(safe, "pyproject.toml", in_body_drivers=_DRIVER) == []


def test_noop_without_driver_context() -> None:
    # Text-only caller (no filesystem context) never fires (mirrors T018).
    assert scan_text(_RISKY_CFG, "pyproject.toml") == []


def test_anchored_on_fixture_scope_line() -> None:
    findings = scan_text(_RISKY_CFG, "pyproject.toml", in_body_drivers=_DRIVER)
    assert findings[0].line == 3  # the asyncio_default_fixture_loop_scope line


def test_suppressed_on_fixture_scope_line() -> None:
    text = (
        "[tool.pytest.ini_options]\n"
        'asyncio_default_fixture_loop_scope = "session"  '
        "# conformance: ignore[T019] every async fixture is loop-agnostic\n"
    )
    findings = scan_text(text, "pyproject.toml", in_body_drivers=_DRIVER)
    assert [f.rule_id for f in findings] == ["T019"]
    assert findings[0].suppressed is True
    assert (
        findings[0].suppression_justification == "every async fixture is loop-agnostic"
    )


# ── Filesystem end-to-end via scan_path ──────────────────────────────────────


def _write(root: Path, pyproject: str, test_rel: str, test_src: str) -> None:
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    p = root / test_rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(test_src, encoding="utf-8")


def test_scan_path_fires_on_in_body_driver_repo(tmp_path: Path) -> None:
    # HANG-RISK shape (microstrategy): await execute_app in a test body.
    _write(
        tmp_path,
        _RISKY_CFG,
        "tests/integration/test_conn.py",
        "async def test_extraction(executor):\n"
        "    result = await executor.execute_app(App, inp)\n"
        "    assert result\n",
    )
    findings = scan_path(tmp_path / "pyproject.toml", tmp_path)
    assert [f.rule_id for f in findings] == ["T019"]
    assert "tests/integration/test_conn.py::test_extraction" in findings[0].message


def test_scan_path_silent_on_fixture_only_repo(tmp_path: Path) -> None:
    # CONFIG-ONLY shape (metabase/mysql): execution in a fixture, tests assert.
    _write(
        tmp_path,
        _RISKY_CFG,
        "tests/integration/test_conn.py",
        "import pytest\n\n"
        '@pytest.fixture(scope="class")\n'
        "async def extraction_result(executor):\n"
        "    return await executor.execute_app(App, inp)\n\n"
        "def test_completes(extraction_result):\n"
        "    assert extraction_result is not None\n",
    )
    assert scan_path(tmp_path / "pyproject.toml", tmp_path) == []


def test_scan_path_silent_when_scopes_consistent(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "[tool.pytest.ini_options]\n"
        'asyncio_default_fixture_loop_scope = "session"\n'
        'asyncio_default_test_loop_scope = "session"\n',
        "tests/integration/test_conn.py",
        "async def test_extraction(executor):\n"
        "    assert await executor.execute_app(App, inp)\n",
    )
    assert scan_path(tmp_path / "pyproject.toml", tmp_path) == []


def test_in_body_workflow_drivers_finds_class_methods(tmp_path: Path) -> None:
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    (tmp_path / "tests" / "integration" / "test_conn.py").write_text(
        "class TestX:\n"
        "    async def test_a(self, ex):\n"
        "        assert await ex.execute_workflow(w)\n"
        "    async def test_b(self, extraction_result):\n"
        "        assert extraction_result\n",
        encoding="utf-8",
    )
    labels = in_body_workflow_drivers(tmp_path)
    assert labels == ["tests/integration/test_conn.py::TestX::test_a"]


def test_scan_path_noop_without_pyproject(tmp_path: Path) -> None:
    assert scan_path(tmp_path / "pyproject.toml", tmp_path) == []
