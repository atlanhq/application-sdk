"""Tests for conformance.tools.ledger_guard (the CI append-only ledger guard)."""

from __future__ import annotations

from conformance.tools.ledger_guard import _index_fields, check

# ── _index_fields ─────────────────────────────────────────────────────────────


def test_index_fields_empty() -> None:
    assert _index_fields({}) == {}
    assert _index_fields({"fields": []}) == {}


def test_index_fields_basic() -> None:
    payload = {
        "fields": [
            {"contract": "MyInput", "field": "name", "type": "str", "status": "active"},
            {
                "contract": "MyInput",
                "field": "count",
                "type": "int",
                "status": "active",
            },
        ]
    }
    idx = _index_fields(payload)
    assert idx == {
        ("MyInput", "name"): "str",
        ("MyInput", "count"): "int",
    }


# ── check: no base (new ledger) ────────────────────────────────────────────────


def test_no_base_passes() -> None:
    """No prior ledger means nothing to guard — always passes."""
    head = {
        "version": 1,
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "active"}],
    }
    passed, errors = check(None, head)
    assert passed
    assert errors == []


def test_no_base_no_head_passes() -> None:
    passed, errors = check(None, None)
    assert passed
    assert errors == []


# ── check: deletion blocked ───────────────────────────────────────────────────


def test_deletion_blocked() -> None:
    base = {
        "fields": [
            {"contract": "MyInput", "field": "url", "type": "str", "status": "active"}
        ]
    }
    head = {"fields": []}
    passed, errors = check(base, head)
    assert not passed
    assert any("DELETED" in e and "MyInput.url" in e for e in errors)


def test_deletion_blocked_multi_field() -> None:
    base = {
        "fields": [
            {"contract": "X", "field": "a", "type": "str", "status": "active"},
            {"contract": "X", "field": "b", "type": "int", "status": "active"},
        ]
    }
    head = {
        "fields": [{"contract": "X", "field": "a", "type": "str", "status": "active"}]
    }
    passed, errors = check(base, head)
    assert not passed
    assert any("DELETED" in e and "X.b" in e for e in errors)


# ── check: type change blocked ────────────────────────────────────────────────


def test_type_change_blocked() -> None:
    base = {
        "fields": [
            {"contract": "MyInput", "field": "count", "type": "str", "status": "active"}
        ]
    }
    head = {
        "fields": [
            {"contract": "MyInput", "field": "count", "type": "int", "status": "active"}
        ]
    }
    passed, errors = check(base, head)
    assert not passed
    assert any("TYPE CHANGED" in e and "MyInput.count" in e for e in errors)


def test_type_change_carries_both_types() -> None:
    base = {
        "fields": [{"contract": "C", "field": "f", "type": "str", "status": "active"}]
    }
    head = {
        "fields": [
            {"contract": "C", "field": "f", "type": "list[str]", "status": "active"}
        ]
    }
    passed, errors = check(base, head)
    assert not passed
    assert any("'str'" in e and "'list[str]'" in e for e in errors)


# ── check: status change allowed ─────────────────────────────────────────────


def test_status_change_allowed() -> None:
    base = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "active"}]
    }
    head = {
        "fields": [
            {"contract": "X", "field": "f", "type": "str", "status": "deprecated"}
        ]
    }
    passed, errors = check(base, head)
    assert passed
    assert errors == []


def test_status_change_to_sunset_allowed() -> None:
    base = {
        "fields": [
            {"contract": "X", "field": "f", "type": "str", "status": "deprecated"}
        ]
    }
    head = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "sunset"}]
    }
    passed, errors = check(base, head)
    assert passed
    assert errors == []


# ── check: addition allowed ───────────────────────────────────────────────────


def test_addition_allowed() -> None:
    base = {
        "fields": [{"contract": "X", "field": "a", "type": "str", "status": "active"}]
    }
    head = {
        "fields": [
            {"contract": "X", "field": "a", "type": "str", "status": "active"},
            {"contract": "X", "field": "b", "type": "int", "status": "active"},
        ]
    }
    passed, errors = check(base, head)
    assert passed
    assert errors == []


def test_head_absent_when_base_exists_blocked() -> None:
    base = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "active"}]
    }
    passed, errors = check(base, None)
    assert not passed
    assert any("deleted" in e.lower() or "absent" in e.lower() for e in errors)


# ── check: multiple violations in one run ─────────────────────────────────────


def test_multiple_violations_all_reported() -> None:
    base = {
        "fields": [
            {"contract": "A", "field": "x", "type": "str", "status": "active"},
            {"contract": "B", "field": "y", "type": "int", "status": "active"},
        ]
    }
    head = {
        "fields": [
            {"contract": "B", "field": "y", "type": "str", "status": "active"},
        ]
    }
    passed, errors = check(base, head)
    assert not passed
    assert len(errors) == 2
    assert any("DELETED" in e for e in errors)
    assert any("TYPE CHANGED" in e for e in errors)


# ── check: sunset requires a prior deprecation ───────────────────────────────


def test_active_to_sunset_blocked() -> None:
    """Skipping 'deprecated' fails and names the field and the missing step."""
    base = {
        "fields": [
            {"contract": "MyInput", "field": "url", "type": "str", "status": "active"}
        ]
    }
    head = {
        "fields": [
            {"contract": "MyInput", "field": "url", "type": "str", "status": "sunset"}
        ]
    }
    passed, errors = check(base, head)
    assert not passed
    assert len(errors) == 1
    assert "MyInput.url" in errors[0]
    assert "'active' → 'sunset'" in errors[0]
    assert "'deprecated' first" in errors[0]


def test_absent_status_counts_as_active() -> None:
    """A base row without 'status' reads as active, as the ledger loader does."""
    base = {"fields": [{"contract": "X", "field": "f", "type": "str"}]}
    head = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "sunset"}]
    }
    passed, errors = check(base, head)
    assert not passed
    assert "X.f" in errors[0]


def test_active_deprecated_sunset_across_prs_passes() -> None:
    """The intended path passes at each step when the steps land separately."""
    rows = [
        {"fields": [{"contract": "X", "field": "f", "type": "str", "status": s}]}
        for s in ("active", "deprecated", "sunset")
    ]
    for base, head in zip(rows, rows[1:]):
        passed, errors = check(base, head)
        assert passed, errors


def test_new_entry_recorded_as_sunset_allowed() -> None:
    """An entry absent from base has no prior status to skip over."""
    base: dict = {"fields": []}
    head = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "sunset"}]
    }
    passed, errors = check(base, head)
    assert passed
    assert errors == []


def test_reactivation_allowed() -> None:
    """Moving back toward 'active' restores a field, which is additive."""
    base = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "sunset"}]
    }
    head = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "active"}]
    }
    passed, errors = check(base, head)
    assert passed
    assert errors == []
