"""Tests for .github/scripts/validate_allowlist.py."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import validate_allowlist


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


def _entry(**overrides: Any) -> dict[str, Any]:
    base = {
        "package": "example-pkg",
        "severity": "HIGH",
        "reason": "Case 2: no fix yet; vulnerable path not exercised.",
        "expires": _future(20),
        "added_by": "@atlan-ci",
        "case": 2,
        "ticket": "BLDX-1234",
    }
    base.update(overrides)
    return base


def _run(entries: dict[str, Any], tmp_path: Path, monkeypatch, policy=None) -> int:
    data: dict[str, Any] = {"_expiry_policy": policy or {"CRITICAL": 7, "HIGH": 30}}
    data.update(entries)
    p = tmp_path / "base-allowlist.json"
    p.write_text(json.dumps(data))
    monkeypatch.setattr(validate_allowlist, "ALLOWLIST_PATH", str(p))
    return validate_allowlist.main()


def test_valid_high_entry_passes(tmp_path, monkeypatch):
    assert _run({"CVE-2026-1": _entry()}, tmp_path, monkeypatch) == 0


def test_valid_critical_within_7_days_passes(tmp_path, monkeypatch):
    entry = _entry(severity="CRITICAL", expires=_future(6), case=1)
    assert _run({"CVE-2026-2": entry}, tmp_path, monkeypatch) == 0


def test_critical_beyond_7_days_fails(tmp_path, monkeypatch):
    entry = _entry(severity="CRITICAL", expires=_future(10))
    assert _run({"CVE-2026-3": entry}, tmp_path, monkeypatch) == 1


def test_medium_severity_rejected(tmp_path, monkeypatch):
    # MEDIUM/LOW are never allowlisted — unknown severity in policy.
    entry = _entry(severity="MEDIUM")
    assert _run({"CVE-2026-4": entry}, tmp_path, monkeypatch) == 1


def test_missing_case_fails(tmp_path, monkeypatch):
    entry = _entry()
    del entry["case"]
    assert _run({"CVE-2026-5": entry}, tmp_path, monkeypatch) == 1


def test_missing_ticket_fails(tmp_path, monkeypatch):
    entry = _entry()
    del entry["ticket"]
    assert _run({"CVE-2026-6": entry}, tmp_path, monkeypatch) == 1


def test_invalid_case_value_fails(tmp_path, monkeypatch):
    entry = _entry(case=5)
    assert _run({"CVE-2026-7": entry}, tmp_path, monkeypatch) == 1


def test_case_as_string_accepted(tmp_path, monkeypatch):
    entry = _entry(case="3")
    assert _run({"CVE-2026-8": entry}, tmp_path, monkeypatch) == 0


def test_fix_pr_must_be_https(tmp_path, monkeypatch):
    entry = _entry(case=1, fix_pr="#1234")
    assert _run({"CVE-2026-9": entry}, tmp_path, monkeypatch) == 1


def test_valid_fix_pr_passes(tmp_path, monkeypatch):
    entry = _entry(case=1, fix_pr="https://github.com/atlanhq/application-sdk/pull/1")
    assert _run({"CVE-2026-10": entry}, tmp_path, monkeypatch) == 0


def test_already_expired_fails(tmp_path, monkeypatch):
    entry = _entry(expires=_future(-1))
    assert _run({"CVE-2026-11": entry}, tmp_path, monkeypatch) == 1


def test_empty_allowlist_passes(tmp_path, monkeypatch):
    assert _run({}, tmp_path, monkeypatch) == 0


def test_entry_schema_doc_key_is_ignored(tmp_path, monkeypatch):
    # The "_entry_schema" documentation key must not be validated as an entry.
    data = {
        "_expiry_policy": {"CRITICAL": 7, "HIGH": 30},
        "_entry_schema": {"_doc": "x", "CVE-0000": {"severity": "MEDIUM"}},
    }
    p = tmp_path / "base-allowlist.json"
    p.write_text(json.dumps(data))
    monkeypatch.setattr(validate_allowlist, "ALLOWLIST_PATH", str(p))
    assert validate_allowlist.main() == 0
