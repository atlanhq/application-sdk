"""Tests for the GHA expression evaluator that test_label_trigger_gates.py uses.

The gate guards are only as trustworthy as this evaluator, so its own semantics
are pinned here — especially the ones that differ from Python's: case-insensitive
string equality, `&&`/`||` returning an operand rather than a boolean, `'false'`
being truthy, and NaN comparing false against everything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import (  # noqa: E402
    UnknownContext,
    UnsupportedExpression,
    evaluate,
    evaluate_operand,
    truthy,
)


def _eval(expression: str, **contexts: object) -> bool:
    return evaluate(expression, dict(contexts))


# ── Literals and truthiness ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "negated"),
    [
        # `negated` is the value of `!x.v`, i.e. the opposite of truthiness.
        (None, True),
        (False, True),
        (0, True),
        ("", True),
        ("false", False),  # the classic footgun: a non-empty string is truthy
        ("0", False),
        ([], False),  # an empty array is an object, and objects are truthy
        ({}, False),
    ],
)
def test_truthiness_matches_githubs_cast_not_pythons(
    value: object, negated: bool
) -> None:
    assert _eval("!x.v", x={"v": value}) is negated


def test_a_bare_context_is_its_own_condition() -> None:
    assert _eval("inputs.flag", inputs={"flag": True}) is True
    assert _eval("inputs.flag", inputs={"flag": False}) is False
    # A workflow_dispatch input arrives as a string, so "false" runs the job.
    assert _eval("inputs.flag", inputs={"flag": "false"}) is True


def test_truthy_is_exported_for_callers_inspecting_operand_results() -> None:
    assert truthy([]) is True
    assert truthy("") is False


# ── Equality ─────────────────────────────────────────────────────────────────


def test_string_equality_is_case_insensitive() -> None:
    assert _eval("github.ref == 'REFS/HEADS/MAIN'", github={"ref": "refs/heads/main"})


def test_missing_property_is_null_and_compares_equal_to_false_and_zero() -> None:
    assert _eval("github.event.nope == false", github={"event": {}})
    assert _eval("github.event.nope == 0", github={"event": {}})
    # …and unequal to true, which is what the `fork != true` gate term relies on
    # for a payload where `fork` is simply absent.
    assert _eval("github.event.nope != true", github={"event": {}})


def test_fork_false_and_fork_absent_both_pass_the_fork_guard() -> None:
    for repo in ({"fork": False}, {}):
        assert _eval("g.repo.fork != true", g={"repo": repo}), repo
    assert not _eval("g.repo.fork != true", g={"repo": {"fork": True}})


def test_non_numeric_string_compares_false_against_a_number() -> None:
    # Mismatched types cast to number; a non-numeric string yields NaN, and NaN
    # is equal to nothing. Note this only bites across types — two strings take
    # the string path and compare normally, however non-numeric they are.
    assert not _eval("x.a == 1", x={"a": "abc"})
    assert not _eval("x.a == true", x={"a": "abc"})
    assert _eval("x.a == x.a", x={"a": "abc"})


def test_arrays_compare_by_reference_not_content() -> None:
    shared: list[str] = ["a"]
    assert _eval("x.a == x.b", x={"a": shared, "b": shared})
    assert not _eval("x.a == x.b", x={"a": ["a"], "b": ["a"]})


# ── Operators ────────────────────────────────────────────────────────────────


def test_and_binds_tighter_than_or() -> None:
    # false && false || true  →  (false && false) || true  →  true
    assert _eval("x.f && x.f || x.t", x={"f": False, "t": True})
    # If precedence were the other way this would be false.
    assert _eval("x.t || x.f && x.f", x={"f": False, "t": True})


def test_parentheses_override_precedence() -> None:
    assert not _eval("x.t && (x.f || x.f)", x={"f": False, "t": True})


def test_and_or_return_an_operand_so_a_falsy_right_hand_side_wins() -> None:
    # `'' || 'fallback'` is the documented default-value idiom.
    assert _eval("x.empty || x.fallback", x={"empty": "", "fallback": "v"})
    assert not _eval("x.set && x.empty", x={"set": "v", "empty": ""})


def test_and_or_return_the_selected_operand_not_a_boolean() -> None:
    # Value-level: pin the operand itself, not just its truthiness, so an
    # evaluator that returned booleans instead would fail here even though it
    # would still pass the truthy-only assertions above.
    assert (
        evaluate_operand("x.empty || x.fallback", {"x": {"empty": "", "fallback": "v"}})
        == "v"
    )
    assert evaluate_operand("x.set && x.empty", {"x": {"set": "v", "empty": ""}}) == ""
    assert (
        evaluate_operand("x.set || x.fallback", {"x": {"set": "v", "fallback": "w"}})
        == "v"
    )
    assert (
        evaluate_operand("x.set && x.other", {"x": {"set": "v", "other": "w"}}) == "w"
    )


def test_not_yields_a_boolean() -> None:
    assert _eval("!x.empty", x={"empty": ""})
    assert not _eval("!x.set", x={"set": "v"})


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("x.a < x.b", True),
        ("x.a > x.b", False),
        ("x.a <= x.a", True),
        ("x.b >= x.a", True),
    ],
)
def test_ordering_comparisons(expression: str, expected: bool) -> None:
    assert _eval(expression, x={"a": 1, "b": 2}) is expected


# ── Property access, object filters, indexing ────────────────────────────────


def test_object_filter_collects_a_property_across_an_array() -> None:
    payload = {"labels": [{"name": "e2e"}, {"name": "size/M"}]}
    assert _eval("pr.labels.*.name != null", pr=payload)
    assert _eval("contains(pr.labels.*.name, 'size/M')", pr=payload)


def test_object_filter_on_an_empty_array_matches_nothing() -> None:
    assert not _eval("contains(pr.labels.*.name, 'e2e')", pr={"labels": []})


def test_property_access_through_a_null_stays_null_rather_than_erroring() -> None:
    # `github.event.label.name` on a non-`labeled` event: `label` is absent.
    assert _eval("github.event.label.name != 'e2e'", github={"event": {}})


def test_index_access_on_arrays_and_objects() -> None:
    assert _eval("x.a[1] == 'b'", x={"a": ["a", "b"]})
    assert _eval("x.a['k'] == 'v'", x={"a": {"k": "v"}})
    assert _eval("x.a[9] == null", x={"a": ["a"]})


# ── Functions ────────────────────────────────────────────────────────────────


def test_contains_is_membership_on_arrays_not_substring() -> None:
    labels = {"labels": [{"name": "e2e-full"}]}
    assert not _eval("contains(pr.labels.*.name, 'e2e')", pr=labels)
    assert _eval("contains(pr.labels.*.name, 'e2e-full')", pr=labels)


def test_contains_is_substring_on_strings() -> None:
    assert _eval("contains(github.ref, 'heads')", github={"ref": "refs/heads/main"})


def test_string_helpers_are_case_insensitive() -> None:
    assert _eval("startsWith(github.ref, 'REFS/')", github={"ref": "refs/heads/main"})
    assert _eval("endsWith(github.ref, 'MAIN')", github={"ref": "refs/heads/main"})


def test_always_is_modelled_but_success_is_not() -> None:
    assert _eval("always()")
    # `success()` reflects real upstream job results. Modelling it would let a
    # gate scenario pass without ever exercising the condition under test.
    with pytest.raises(UnsupportedExpression, match="success"):
        _eval("success()")
    with pytest.raises(UnsupportedExpression, match="failure"):
        _eval("failure()")


def test_cancelled_is_modelled_because_it_carries_no_gate_meaning() -> None:
    """`!cancelled()` only keeps a job alive past a skipped `needs`.

    Unlike `success()`, it says nothing about whether the gate's real condition
    holds, so fixing it at False lets a test reach the clause that matters.
    """
    assert _eval("cancelled()") is False
    assert _eval(
        "!cancelled() && needs.a.outputs.x == 'ok'",
        needs={"a": {"outputs": {"x": "ok"}}},
    )
    assert not _eval(
        "!cancelled() && needs.a.outputs.x == 'ok'",
        needs={"a": {"outputs": {"x": "no"}}},
    )


def test_format_and_join() -> None:
    assert _eval("format('{0}-{1}', 'a', 'b') == 'a-b'")
    assert _eval("join(x.a, '-') == 'a-b'", x={"a": ["a", "b"]})


# ── Refusals ─────────────────────────────────────────────────────────────────


def test_an_unsupplied_context_root_raises_instead_of_resolving_to_null() -> None:
    """The property that stops a scenario from passing vacuously."""
    with pytest.raises(UnknownContext, match="needs"):
        _eval("needs.build.result == 'success'", github={})


def test_unknown_functions_and_unparseable_input_raise() -> None:
    with pytest.raises(UnsupportedExpression, match="fromJSON"):
        _eval("fromJSON('[]')")
    with pytest.raises(UnsupportedExpression):
        _eval("x.a ===== x.b", x={})
    with pytest.raises(UnsupportedExpression):
        _eval("(x.a", x={})
    with pytest.raises(UnsupportedExpression, match="trailing"):
        _eval("x.a x.b", x={})


def test_the_optional_surrounding_interpolation_is_stripped() -> None:
    assert _eval("${{ github.ref == 'main' }}", github={"ref": "main"})
    with pytest.raises(UnsupportedExpression, match="partially-interpolated"):
        _eval("a-${{ github.ref }}-b", github={"ref": "main"})


def test_quotes_are_unescaped_by_doubling() -> None:
    assert _eval("x.a == 'it''s'", x={"a": "it's"})
