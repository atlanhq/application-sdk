"""Tests for conformance.tools.ledger_guard (the CI append-only ledger guard)."""

from __future__ import annotations

import pytest
from conformance.tools.ledger_guard import _index_fields, _index_statuses, check

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


@pytest.mark.parametrize(
    ("value", "prefix"),
    [
        ({"required": "yes"}, "INVALID REQUIRED"),
        ({"status": "retired"}, "INVALID STATUS"),
    ],
    ids=["required", "status"],
)
def test_no_base_still_rejects_hand_edited_values(value: dict, prefix: str) -> None:
    """A first ledger has nothing to compare against, but its values are checked."""
    head = {"fields": [{"contract": "X", "field": "f", "type": "str"} | value]}
    passed, errors = check(None, head)
    assert not passed
    assert [e.split(":")[0] for e in errors] == [prefix]


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


@pytest.mark.parametrize(
    "base_row",
    [
        {"contract": "X", "field": "f", "type": "str"},
        {"contract": "X", "field": "f", "type": "str", "status": None},
    ],
    ids=["absent", "null"],
)
def test_absent_or_null_status_counts_as_active(base_row: dict) -> None:
    """A base row without a status reads as active, as the ledger loader does."""
    base = {"fields": [base_row]}
    head = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "sunset"}]
    }
    passed, errors = check(base, head)
    assert not passed
    assert "X.f" in errors[0]


def test_null_head_status_is_active_not_a_bypass() -> None:
    """A null HEAD status is 'active' — not a status that slips past every rule."""
    base = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "active"}]
    }
    head = {"fields": [{"contract": "X", "field": "f", "type": "str", "status": None}]}
    assert _index_statuses(head) == {("X", "f"): "active"}
    passed, errors = check(base, head)
    assert passed, errors


def test_unknown_head_status_blocked() -> None:
    base = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "active"}]
    }
    head = {
        "fields": [{"contract": "X", "field": "f", "type": "str", "status": "retired"}]
    }
    passed, errors = check(base, head)
    assert not passed
    assert errors == [
        "INVALID STATUS: X.f 'retired' — a ledger status is one of 'active', "
        "'deprecated' or 'sunset'; regenerate instead of editing it by hand."
    ]


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


# ── check: requiredness ───────────────────────────────────────────────────────


def _row(field: str, required: object = False, contract: str = "MyInput") -> dict:
    row: dict = {"contract": contract, "field": field, "type": "str"}
    if required is not None:
        row["required"] = required
    return row


def test_new_required_field_on_existing_contract_blocked() -> None:
    """A required entry added to a contract the base records fails, naming it."""
    base = {"fields": [_row("name")]}
    head = {"fields": [_row("name"), _row("extra", True)]}
    passed, errors = check(base, head)
    assert not passed
    assert len(errors) == 1
    assert errors[0].startswith("NEW REQUIRED FIELD: MyInput.extra")


def test_new_field_with_default_on_existing_contract_allowed() -> None:
    base = {"fields": [_row("name")]}
    head = {"fields": [_row("name"), _row("extra", False)]}
    assert check(base, head) == (True, [])


def test_new_contract_may_add_required_fields() -> None:
    base = {"fields": [_row("name")]}
    head = {"fields": [_row("name"), _row("x", True, contract="NewInput")]}
    assert check(base, head) == (True, [])


def test_optional_to_required_blocked() -> None:
    base = {"fields": [_row("extra", False)]}
    head = {"fields": [_row("extra", True)]}
    passed, errors = check(base, head)
    assert not passed
    assert len(errors) == 1
    assert errors[0].startswith("NEWLY REQUIRED: MyInput.extra")


@pytest.mark.parametrize(
    ("base_required", "head_required"),
    [(None, True), (None, False), (True, False), (True, True), (False, False)],
    ids=["backfill-required", "backfill-optional", "relax", "keep", "keep-optional"],
)
def test_requiredness_moves_that_break_no_caller_allowed(
    base_required: bool | None, head_required: bool
) -> None:
    """An unknown base (a version-1 ledger) may backfill to either value."""
    base = {"fields": [_row("extra", base_required)]}
    head = {"fields": [_row("extra", head_required)]}
    assert check(base, head) == (True, [])


@pytest.mark.parametrize("base_required", [False, True], ids=["false", "true"])
def test_erasing_a_known_required_blocked(base_required: bool) -> None:
    """A known baseline cannot become unknown: unknown is never checked."""
    base = {"fields": [_row("extra", base_required)]}
    head = {"fields": [_row("extra", None)]}
    passed, errors = check(base, head)
    assert not passed
    assert len(errors) == 1
    assert errors[0].startswith("REQUIRED ERASED: MyInput.extra")


def test_invalid_head_required_blocked() -> None:
    base = {"fields": [_row("extra")]}
    head = {"fields": [_row("extra", "yes")]}
    passed, errors = check(base, head)
    assert not passed
    assert errors == [
        "INVALID REQUIRED: MyInput.extra 'yes' — a ledger 'required' is true, "
        "false or absent; regenerate instead of editing it by hand."
    ]
