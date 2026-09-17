"""Unit tests for the attribute matchers and ``evaluate_attributes`` (FND-2094).

The distinction under test throughout is the one the feature exists for:
*present-but-zero* is not *absent*. A count assertion cannot tell them apart, so
every test that pins a zero has a sibling that drops the attribute entirely and
asserts the two produce different answers.

Pure logic — no tenant, no Atlas. The reader that produces these samples is
covered in ``tests/unit/testing/harness/atlas/test_sample_attributes.py``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from application_sdk.testing.harness.expectations import (
    UNREADABLE,
    Absent,
    AssetAttributes,
    AssetExpectations,
    AtLeast,
    AtMost,
    AttributeMatcher,
    Exactly,
    Present,
    Unreadable,
    as_matcher,
    evaluate_attributes,
    normalise_attribute_expectations,
)
from application_sdk.testing.harness.outcome import (
    Indeterminate,
    Settled,
    as_attribute_samples,
)


def _schema(
    qualified_name: str = "default/x/123/db/sch", **values: object
) -> AssetAttributes:
    """One sampled Schema carrying exactly the attributes named.

    Args:
        qualified_name: The asset's qualifiedName.
        **values: Attributes the search payload carried. An attribute NOT named
            here is absent, which is the whole point.

    Returns:
        The sample.
    """
    return AssetAttributes(qualified_name=qualified_name, values=values)


def _expect(**attributes: object) -> AssetExpectations:
    """Attribute expectations on the ``Schema`` type.

    Args:
        **attributes: Attribute name -> matcher or bare scalar.

    Returns:
        The expectations, with the declaration coerced to matchers exactly as
        the harness coerces a suite's ``expected_asset_attributes``.
    """
    return AssetExpectations(
        attributes=normalise_attribute_expectations({"Schema": attributes})
    )


# ---------------------------------------------------------------------------
# Exactly
# ---------------------------------------------------------------------------


def test_exact_value_matches() -> None:
    findings = evaluate_attributes(
        {"Schema": [_schema(tableCount=8)]}, _expect(tableCount=8)
    )
    assert findings == []


def test_exact_value_mismatch_names_asset_attribute_and_both_values() -> None:
    findings = evaluate_attributes(
        {"Schema": [_schema(tableCount=0)]}, _expect(tableCount=8)
    )
    assert [(f.subject, f.expectation) for f in findings] == [
        ("Schema.tableCount", "attribute")
    ]
    assert "default/x/123/db/sch" in findings[0].detail
    assert "= 0" in findings[0].detail
    assert "expected exactly 8" in findings[0].detail


def test_expecting_zero_passes_when_the_value_is_zero() -> None:
    # viewsCount == 0 is a real claim a connector makes, not a placeholder.
    findings = evaluate_attributes(
        {"Schema": [_schema(viewsCount=0)]}, _expect(viewsCount=0)
    )
    assert findings == []


def test_expecting_zero_FAILS_when_the_attribute_is_absent() -> None:
    # The motivating regression: an attribute that silently stops being set.
    # A count assertion sees the same "0" both ways; this must not.
    findings = evaluate_attributes({"Schema": [_schema()]}, _expect(viewsCount=0))
    assert len(findings) == 1
    assert "is absent (attribute not set)" in findings[0].detail


def test_zero_does_not_match_false_and_false_does_not_match_zero() -> None:
    assert not Exactly(0).matches(present=True, value=False)
    assert not Exactly(False).matches(present=True, value=0)
    assert Exactly(False).matches(present=True, value=False)


def test_explicit_null_is_present_and_fails_an_exact_value() -> None:
    findings = evaluate_attributes(
        {"Schema": [_schema(tableCount=None)]}, _expect(tableCount=8)
    )
    assert len(findings) == 1
    # Present with None, so the line says the value was None — not that the
    # connector never set it.
    assert "= None" in findings[0].detail


# ---------------------------------------------------------------------------
# Present / Absent
# ---------------------------------------------------------------------------


def test_present_accepts_a_zero() -> None:
    # Present() asks only "is it set at all", so a legitimate zero passes.
    assert (
        evaluate_attributes(
            {"Schema": [_schema(tableCount=0)]}, _expect(tableCount=Present())
        )
        == []
    )


def test_present_rejects_an_absent_attribute() -> None:
    findings = evaluate_attributes(
        {"Schema": [_schema()]}, _expect(tableCount=Present())
    )
    assert len(findings) == 1
    assert "expected a value (present and not null)" in findings[0].detail


def test_present_rejects_an_explicit_null() -> None:
    findings = evaluate_attributes(
        {"Schema": [_schema(tableCount=None)]}, _expect(tableCount=Present())
    )
    assert len(findings) == 1


def test_absent_accepts_an_unset_attribute_and_rejects_a_published_zero() -> None:
    # The inverse pin: "leave it unset rather than claim 0".
    assert (
        evaluate_attributes({"Schema": [_schema()]}, _expect(tableCount=Absent())) == []
    )
    findings = evaluate_attributes(
        {"Schema": [_schema(tableCount=0)]}, _expect(tableCount=Absent())
    )
    assert len(findings) == 1
    assert "expected no value (attribute unset)" in findings[0].detail


def test_absent_rejects_an_explicit_null() -> None:
    # Atlas holding an explicit null is not Atlas holding nothing.
    findings = evaluate_attributes(
        {"Schema": [_schema(tableCount=None)]}, _expect(tableCount=Absent())
    )
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# AtLeast / AtMost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "matches"),
    [(1, True), (1.5, True), (0, False), (-1, False)],
)
def test_at_least_grades_numbers(value: object, matches: bool) -> None:
    findings = evaluate_attributes(
        {"Schema": [_schema(tableCount=value)]}, _expect(tableCount=AtLeast(1))
    )
    assert (findings == []) is matches


def test_at_least_rejects_a_non_number() -> None:
    # An attribute whose type changed is a finding, not a TypeError.
    findings = evaluate_attributes(
        {"Schema": [_schema(tableCount="nine")]}, _expect(tableCount=AtLeast(1))
    )
    assert len(findings) == 1
    assert "expected a number >= 1" in findings[0].detail


def test_at_least_rejects_a_bool() -> None:
    # True is an int to Python; matching AtLeast(1) would be a type-system
    # coincidence rather than an assertion anyone wrote.
    assert not AtLeast(1).matches(present=True, value=True)


def test_at_least_rejects_an_absent_attribute() -> None:
    findings = evaluate_attributes(
        {"Schema": [_schema()]}, _expect(tableCount=AtLeast(1))
    )
    assert len(findings) == 1
    assert "is absent" in findings[0].detail


def test_at_most_grades_the_ceiling() -> None:
    assert (
        evaluate_attributes(
            {"Schema": [_schema(tableCount=8)]}, _expect(tableCount=AtMost(8))
        )
        == []
    )
    findings = evaluate_attributes(
        {"Schema": [_schema(tableCount=9)]}, _expect(tableCount=AtMost(8))
    )
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# Sampling semantics
# ---------------------------------------------------------------------------


def test_every_sampled_asset_must_satisfy_every_matcher() -> None:
    samples = {
        "Schema": [
            _schema("default/x/123/db/good", tableCount=8),
            _schema("default/x/123/db/bad", tableCount=0),
        ]
    }
    findings = evaluate_attributes(samples, _expect(tableCount=8))
    assert len(findings) == 1
    assert "default/x/123/db/bad" in findings[0].detail


def test_all_unmet_matchers_on_one_asset_are_reported() -> None:
    findings = evaluate_attributes(
        {"Schema": [_schema(tableCount=0, viewsCount=0)]},
        _expect(tableCount=8, viewsCount=1),
    )
    assert [f.subject for f in findings] == ["Schema.tableCount", "Schema.viewsCount"]


def test_an_empty_sample_is_skipped() -> None:
    # "The type landed nothing" is the count floors' job, exactly as for depths.
    assert evaluate_attributes({"Schema": []}, _expect(tableCount=8)) == []


def test_a_type_absent_from_the_samples_is_skipped() -> None:
    assert evaluate_attributes({}, _expect(tableCount=8)) == []


def test_undeclared_types_in_the_sample_are_ignored() -> None:
    samples = {
        "Schema": [_schema(tableCount=8)],
        "Database": [_schema("default/x/123/db", schemaCount=0)],
    }
    assert evaluate_attributes(samples, _expect(tableCount=8)) == []


def test_no_declaration_is_a_noop() -> None:
    samples = {"Schema": [_schema(tableCount=0)]}
    assert evaluate_attributes(samples, AssetExpectations()) == []


# ---------------------------------------------------------------------------
# Unreadable: could not grade is never "the connector regressed"
# ---------------------------------------------------------------------------


def test_an_unreadable_sample_is_reported_as_ungraded_not_as_a_wrong_value() -> None:
    cause = RuntimeError("atlas 503")
    findings = evaluate_attributes(
        {"Schema": Unreadable(cause=cause)}, _expect(tableCount=8)
    )
    assert [f.expectation for f in findings] == [UNREADABLE]
    assert "attribute expectation was not graded" in findings[0].detail
    assert "atlas 503" in findings[0].detail


def test_as_attribute_samples_spreads_an_unreadable_read_over_every_type() -> None:
    reading: Indeterminate[object] = Indeterminate(
        label="attrs",
        attempts=1,
        elapsed=timedelta(0),
        cause=RuntimeError("boom"),
    )
    projected = as_attribute_samples(reading, ("Schema", "Database"))
    assert set(projected) == {"Schema", "Database"}
    assert all(isinstance(value, Unreadable) for value in projected.values())


def test_as_attribute_samples_passes_a_settled_read_through() -> None:
    reading = Settled(
        label="attrs",
        attempts=1,
        elapsed=timedelta(0),
        value={"Schema": [_schema(tableCount=8)]},
    )
    projected = as_attribute_samples(reading, ("Schema",))
    assert projected == {"Schema": [_schema(tableCount=8)]}


# ---------------------------------------------------------------------------
# Declaration sugar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scalar", [8, 0, "gold", True, None, 1.5])
def test_a_bare_scalar_becomes_an_exactly_matcher(scalar: object) -> None:
    assert as_matcher(scalar) == Exactly(scalar)


def test_a_matcher_is_passed_through_unchanged() -> None:
    matcher: AttributeMatcher = AtLeast(3)
    assert as_matcher(matcher) is matcher


def test_normalise_coerces_every_declared_value() -> None:
    normalised = normalise_attribute_expectations(
        {"Schema": {"tableCount": 8, "viewsCount": Present()}}
    )
    assert normalised == {"Schema": {"tableCount": Exactly(8), "viewsCount": Present()}}
