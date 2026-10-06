"""Tests for .github/scripts/assert_app_token_slug.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import assert_app_token_slug as slug  # noqa: E402

EXPECTED = "atlan-conformance-sync"


def test_matching_slug_passes(monkeypatch):
    monkeypatch.setenv("APP_SLUG", EXPECTED)
    assert slug.main([EXPECTED]) == 0


def test_another_apps_token_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("APP_SLUG", "atlan-app-fleet")
    assert slug.main([EXPECTED]) == 1
    assert "atlan-app-fleet" in capsys.readouterr().err


def test_missing_slug_is_refused(monkeypatch):
    monkeypatch.delenv("APP_SLUG", raising=False)
    assert slug.main([EXPECTED]) == 1


def test_missing_expected_slug_is_a_usage_error(monkeypatch):
    monkeypatch.setenv("APP_SLUG", EXPECTED)
    assert slug.main([]) == 2
    assert slug.main([""]) == 2
