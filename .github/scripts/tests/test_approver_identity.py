"""Tests for .github/scripts/approver_identity.py — who posts SDK approvals."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import approver_identity as ident


def test_defaults_to_atlan_ci_when_unset():
    assert ident.approver_login({}) == "atlan-ci"


def test_blank_value_falls_back_to_atlan_ci():
    # `vars.SDK_APPROVER_LOGIN || 'atlan-ci'` renders the fallback, but a caller
    # passing an empty or whitespace string must not yield an empty login.
    assert ident.approver_login({"APPROVER_LOGIN": "  "}) == "atlan-ci"


def test_uses_the_configured_login():
    assert ident.approver_login({"APPROVER_LOGIN": "sdk-approver"}) == "sdk-approver"


def test_logins_always_include_the_legacy_account():
    assert ident.approver_logins({"APPROVER_LOGIN": "sdk-approver"}) == {
        "sdk-approver",
        "atlan-ci",
    }
    assert ident.approver_logins({}) == {"atlan-ci"}
