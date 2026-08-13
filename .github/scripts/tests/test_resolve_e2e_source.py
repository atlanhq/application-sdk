"""Tests for resolve_e2e_source.py — the source-selection fail-fast decision.

The decision used to live inline in tests-reusable.yaml's ``run:`` block,
where docs/standards/ci.md forbids branching (it cannot be regression-tested).
These tests pin all four precedence branches plus the workflow-callable entry
point's env-var contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import resolve_e2e_source as res

# ── Precedence (highest first) ──────────────────────────────────────────────────


def test_dataforge_wins_when_datasource_breadcrumb_present():
    code, message = res.decide_source("postgres", "{}", True)
    assert code == 0
    assert message == "e2e source: dataforge (postgres)"


def test_repo_credentials_second():
    code, message = res.decide_source("", '{"X": "y"}', True)
    assert code == 0
    assert "repo credentials" in message


def test_hermetic_fallback_third():
    code, message = res.decide_source("", "", True)
    assert code == 0
    assert "hermetic fallback" in message


def test_no_source_fails_with_actionable_message():
    code, message = res.decide_source("", "", False)
    assert code == 1
    assert "no e2e source available" in message
    # The message must name the fix, not just the symptom.
    assert "E2E_SOURCE_ENV_JSON" in message
    assert "dataforge-hermetic-fallback" in message


def test_no_message_contains_a_credential_value():
    """The selection only ever echoes the datasource breadcrumb — never the
    env map's contents."""
    secret = "s3cret-value"
    for args in (("", secret, False), ("postgres", secret, False), ("", secret, True)):
        _, message = res.decide_source(*args)
        assert secret not in message


# ── main(): the env-var contract the workflow relies on ─────────────────────────


def test_main_reads_selection_from_env(monkeypatch, capsys):
    monkeypatch.setenv("E2E_SOURCE_DATASOURCE", "snowflake")
    monkeypatch.delenv("E2E_SOURCE_ENV_JSON", raising=False)
    assert res.main(["--hermetic-fallback", "false"]) == 0
    assert "::notice::e2e source: dataforge (snowflake)" in capsys.readouterr().err


def test_main_fails_when_no_source(monkeypatch, capsys):
    monkeypatch.delenv("E2E_SOURCE_DATASOURCE", raising=False)
    monkeypatch.delenv("E2E_SOURCE_ENV_JSON", raising=False)
    assert res.main(["--hermetic-fallback", "false"]) == 1
    assert "::error::no e2e source available" in capsys.readouterr().err


def test_main_hermetic_arg_default_comes_from_env(monkeypatch):
    """The workflow passes the flag via the DF_HERMETIC_FALLBACK env var."""
    monkeypatch.delenv("E2E_SOURCE_DATASOURCE", raising=False)
    monkeypatch.delenv("E2E_SOURCE_ENV_JSON", raising=False)
    monkeypatch.setenv("DF_HERMETIC_FALLBACK", "true")
    assert res.main([]) == 0


def test_main_truthiness_is_strict(monkeypatch):
    """Only the literal 'true' enables the hermetic fallback — a stray 'yes'
    or '1' must not silently admit a source-less run."""
    monkeypatch.delenv("E2E_SOURCE_DATASOURCE", raising=False)
    monkeypatch.delenv("E2E_SOURCE_ENV_JSON", raising=False)
    for value in ("yes", "1", ""):
        assert res.main(["--hermetic-fallback", value]) == 1
