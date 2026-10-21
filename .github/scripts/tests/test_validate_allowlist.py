"""Tests for .github/scripts/validate_allowlist.py."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import validate_allowlist
from _frozen_clock import freeze_clock  # noqa: E402

# The validator's clock, frozen for every test (see _frozen_clock). Expiries are
# derived from this same instant, so no test reads the clock twice: the old
# per-test ``datetime.now(UTC)`` sampling failed at 00:10 BST (local 06 Aug,
# UTC still 05 Aug) and again across UTC midnight between two samples.
_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_clock(monkeypatch, validate_allowlist, _NOW)


def _future(days: int) -> str:
    """An expiry ``days`` after the validator's frozen "today"."""
    return (_NOW.date() + timedelta(days=days)).strftime("%Y-%m-%d")


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


def test_expiring_today_is_still_valid(tmp_path, monkeypatch):
    """The expiry boundary is exclusive: `expires == today` has not passed yet.

    Pins the edge directly rather than leaving it implied by the day-either-side
    cases. Without this, an off-by-one in `exp_date < today` — flipping it to
    `<=` — would still pass the whole suite, and the practical effect is an
    allowlist entry being rejected a day early, i.e. a CVE gate firing on an
    entry that is still within policy.
    """
    entry = _entry(expires=_future(0))
    assert _run({"CVE-2026-11a": entry}, tmp_path, monkeypatch) == 0


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
