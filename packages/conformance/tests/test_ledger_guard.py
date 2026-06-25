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
