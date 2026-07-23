"""Meta-tests for the T019 asyncio-loop-scope check.

T019 fires when ``[tool.pytest.ini_options].asyncio_default_fixture_loop_scope``
is set to a broadened scope (``session`` / ``package`` / ``module`` / ``class``)
while ``asyncio_default_test_loop_scope`` is left unset (it defaults to
``function``) — the mismatch that hangs a test which drives a fixture-owned
async resource from the test body.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.asyncio_loop_scope import scan_path, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ── Rule metadata ────────────────────────────────────────────────────────────


def test_t019_rule_metadata() -> None:
    rule = get_rule("T019")
    assert rule.name == "AsyncioTestLoopScopeUnset"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.BOTH
    assert rule.category == "test-async-config"


# ── scan_text: fires on the risky config ─────────────────────────────────────


def test_fires_when_fixture_scope_broadened_and_test_scope_unset() -> None:
    text = (
        "[tool.pytest.ini_options]\n"
        'asyncio_mode = "auto"\n'
        'asyncio_default_fixture_loop_scope = "session"\n'
    )
    findings = scan_text(text, "pyproject.toml")
    assert [f.rule_id for f in findings] == ["T019"]
    # Message names both keys and the silent 'function' default.
    assert "asyncio_default_test_loop_scope" in findings[0].message
    assert "function" in findings[0].message


def test_fires_for_each_broadened_scope() -> None:
    for scope in ("session", "package", "module", "class"):
        text = (
            "[tool.pytest.ini_options]\n"
            f'asyncio_default_fixture_loop_scope = "{scope}"\n'
        )
        findings = scan_text(text, "pyproject.toml")
        assert [f.rule_id for f in findings] == ["T019"], scope
        assert f"'{scope}'" in findings[0].message


# ── scan_text: silent when the config is safe ────────────────────────────────


def test_silent_when_test_scope_set_explicitly() -> None:
    # The author made a deliberate choice → no trap.
    text = (
        "[tool.pytest.ini_options]\n"
        'asyncio_default_fixture_loop_scope = "session"\n'
        'asyncio_default_test_loop_scope = "session"\n'
    )
    assert scan_text(text, "pyproject.toml") == []


def test_silent_when_test_scope_set_to_function_explicitly() -> None:
    # Explicit function test scope is still an explicit, deliberate choice.
    text = (
        "[tool.pytest.ini_options]\n"
        'asyncio_default_fixture_loop_scope = "session"\n'
        'asyncio_default_test_loop_scope = "function"\n'
    )
    assert scan_text(text, "pyproject.toml") == []


def test_silent_when_fixture_scope_is_function() -> None:
    # Fixtures already default to the per-test loop → tests and fixtures agree.
    text = (
        '[tool.pytest.ini_options]\nasyncio_default_fixture_loop_scope = "function"\n'
    )
    assert scan_text(text, "pyproject.toml") == []


def test_silent_when_fixture_scope_unset() -> None:
    # Only asyncio_mode set (SDK / conformance-package shape) → not using the
    # broadened-fixture pattern, nothing to warn about.
    text = '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n'
    assert scan_text(text, "pyproject.toml") == []


def test_silent_when_no_pytest_table() -> None:
    assert scan_text('[project]\nname = "x"\n', "pyproject.toml") == []


def test_silent_on_invalid_scope_value() -> None:
    # A typo pytest-asyncio would itself reject — not this rule's concern.
    text = '[tool.pytest.ini_options]\nasyncio_default_fixture_loop_scope = "sesion"\n'
    assert scan_text(text, "pyproject.toml") == []


def test_silent_on_malformed_toml() -> None:
    assert scan_text("[tool.pytest.ini_options\n = = =", "pyproject.toml") == []


# ── Anchoring + suppression ──────────────────────────────────────────────────


def test_anchored_on_fixture_scope_line() -> None:
    text = (
        "[tool.pytest.ini_options]\n"
        'asyncio_mode = "auto"\n'
        'asyncio_default_fixture_loop_scope = "session"\n'
    )
    findings = scan_text(text, "pyproject.toml")
    assert findings[0].line == 3


def test_suppressed_on_fixture_scope_line() -> None:
    text = (
        "[tool.pytest.ini_options]\n"
        'asyncio_default_fixture_loop_scope = "session"  '
        "# conformance: ignore[T019] every async fixture is loop-agnostic\n"
    )
    findings = scan_text(text, "pyproject.toml")
    assert [f.rule_id for f in findings] == ["T019"]
    assert findings[0].suppressed is True
    assert (
        findings[0].suppression_justification == "every async fixture is loop-agnostic"
    )


# ── Filesystem end-to-end via scan_path ──────────────────────────────────────


def test_scan_path_fires_on_real_repo(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'asyncio_mode = "auto"\n'
        'asyncio_default_fixture_loop_scope = "session"\n',
        encoding="utf-8",
    )
    findings = scan_path(tmp_path / "pyproject.toml", tmp_path)
    assert [f.rule_id for f in findings] == ["T019"]


def test_scan_path_silent_on_consistent_repo(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'asyncio_mode = "auto"\n'
        'asyncio_default_fixture_loop_scope = "session"\n'
        'asyncio_default_test_loop_scope = "session"\n',
        encoding="utf-8",
    )
    assert scan_path(tmp_path / "pyproject.toml", tmp_path) == []


def test_scan_path_noop_without_pyproject(tmp_path: Path) -> None:
    assert scan_path(tmp_path / "pyproject.toml", tmp_path) == []
