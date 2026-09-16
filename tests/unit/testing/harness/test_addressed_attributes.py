"""``evaluate_attributes_at``: pinning a value that differs within a type.

The per-type check asserts one claim about every sampled asset of a type, which
only works where every asset of that type shares the value. Crawl five schemas,
one with no views and one with ten, and the strongest per-type claim left about
``viewsCount`` is ``Present()`` — the numbers themselves are unassertable. These
tests are about the knob that fixes that.

Two behaviours here differ from every other evaluator in the module, and both
are deliberate: a ref matching **nothing** is a finding rather than a skip, and
a ref matching **several** assets is a finding rather than a graded first hit.
"""

from __future__ import annotations

from application_sdk.testing.harness.expectations import (
    UNREADABLE,
    AssetAttributes,
    AssetExpectations,
    AssetRef,
    AtLeast,
    Exactly,
    Present,
    Unreadable,
    evaluate_attributes_at,
    normalise_attribute_expectations_at,
)

_CONN = "default/trino/1700000000"

#: The case the whole knob exists for: same type, different values.
_FIVE_SCHEMAS = {
    "Schema": {
        "sch_empty": {"viewsCount": 0, "tableCount": 3},
        "sch_busy": {"viewsCount": 10, "tableCount": 8},
    }
}


def _expect(
    declared: dict[str, dict[str, dict[str, object]]] | None = None,
) -> AssetExpectations:
    """Per-asset expectations, coerced exactly as the harness coerces them."""
    return AssetExpectations(
        attributes_at=normalise_attribute_expectations_at(declared or _FIVE_SCHEMAS)
    )


def _ref(suffix: str, type_name: str = "Schema") -> AssetRef:
    return AssetRef(type_name=type_name, qualified_name_suffix=suffix)


def _asset(suffix: str, **values: object) -> AssetAttributes:
    return AssetAttributes(qualified_name=f"{_CONN}/db/{suffix}", values=values)


def _both_right() -> dict[AssetRef, list[AssetAttributes]]:
    return {
        _ref("sch_empty"): [_asset("sch_empty", viewsCount=0, tableCount=3)],
        _ref("sch_busy"): [_asset("sch_busy", viewsCount=10, tableCount=8)],
    }


# ---------------------------------------------------------------------------
# The motivating case
# ---------------------------------------------------------------------------


def test_two_schemas_with_different_view_counts_both_pass() -> None:
    assert evaluate_attributes_at(_both_right(), _expect()) == []


def test_the_values_are_pinned_per_asset_not_shared() -> None:
    # Swapping the two schemas' values is invisible to any per-type claim and
    # must not be invisible here.
    reads = {
        _ref("sch_empty"): [_asset("sch_empty", viewsCount=10, tableCount=8)],
        _ref("sch_busy"): [_asset("sch_busy", viewsCount=0, tableCount=3)],
    }
    findings = evaluate_attributes_at(reads, _expect())
    assert [f.subject for f in findings] == [
        "Schema[sch_empty].viewsCount",
        "Schema[sch_empty].tableCount",
        "Schema[sch_busy].viewsCount",
        "Schema[sch_busy].tableCount",
    ]


def test_a_failure_names_the_resolved_qualified_name() -> None:
    reads = _both_right()
    reads[_ref("sch_busy")] = [_asset("sch_busy", viewsCount=0, tableCount=8)]
    findings = evaluate_attributes_at(reads, _expect())
    assert len(findings) == 1
    assert f"{_CONN}/db/sch_busy" in findings[0].detail
    assert "= 0" in findings[0].detail
    assert "expected exactly 10" in findings[0].detail


def test_expecting_zero_on_one_asset_still_fails_on_absence() -> None:
    # The zero-vs-absent distinction survives per-asset addressing too.
    reads = _both_right()
    reads[_ref("sch_empty")] = [_asset("sch_empty", tableCount=3)]
    findings = evaluate_attributes_at(reads, _expect())
    assert len(findings) == 1
    assert "is absent (attribute not set)" in findings[0].detail


def test_matchers_work_per_asset_too() -> None:
    declared = {"Schema": {"sch_busy": {"viewsCount": AtLeast(5)}}}
    reads = {_ref("sch_busy"): [_asset("sch_busy", viewsCount=10)]}
    assert evaluate_attributes_at(reads, _expect(declared)) == []
    reads = {_ref("sch_busy"): [_asset("sch_busy", viewsCount=1)]}
    assert len(evaluate_attributes_at(reads, _expect(declared))) == 1


# ---------------------------------------------------------------------------
# Resolution: nothing, and too much
# ---------------------------------------------------------------------------


def test_a_ref_that_matched_nothing_is_a_finding_not_a_skip() -> None:
    # Unlike the per-type sampler, where an empty sample is the count floors'
    # job. Here the suite named this asset, so its absence is the claim.
    reads: dict[AssetRef, list[AssetAttributes]] = {
        _ref("sch_empty"): [],
        _ref("sch_busy"): [_asset("sch_busy", viewsCount=10, tableCount=8)],
    }
    findings = evaluate_attributes_at(reads, _expect())
    assert [(f.subject, f.expectation) for f in findings] == [
        ("Schema[sch_empty]", "missing")
    ]
    assert "did not land" in findings[0].detail


def test_a_ref_absent_from_the_reads_entirely_is_also_missing() -> None:
    findings = evaluate_attributes_at({}, _expect())
    assert {f.expectation for f in findings} == {"missing"}
    assert len(findings) == 2


def test_an_ambiguous_suffix_is_a_finding_and_lists_the_matches() -> None:
    # Grading the first would make the verdict depend on Atlas's ordering.
    reads = _both_right()
    reads[_ref("sch_empty")] = [
        _asset("sch_empty", viewsCount=0, tableCount=3),
        AssetAttributes(
            qualified_name=f"{_CONN}/db2/sch_empty",
            values={"viewsCount": 4, "tableCount": 1},
        ),
    ]
    findings = evaluate_attributes_at(reads, _expect())
    assert [(f.subject, f.expectation) for f in findings] == [
        ("Schema[sch_empty]", "ambiguous")
    ]
    assert f"{_CONN}/db/sch_empty" in findings[0].detail
    assert f"{_CONN}/db2/sch_empty" in findings[0].detail
    assert "lengthen the suffix" in findings[0].detail


def test_an_ambiguous_ref_does_not_also_report_its_attributes() -> None:
    # One finding, naming the real problem. Grading the attributes of an asset
    # we cannot identify would bury it.
    reads = _both_right()
    reads[_ref("sch_empty")] = [
        _asset("sch_empty", viewsCount=99),
        _asset("sch_empty", viewsCount=99),
    ]
    findings = evaluate_attributes_at(reads, _expect())
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# Unreadable, and no declaration
# ---------------------------------------------------------------------------


def test_an_unreadable_read_is_ungraded_not_a_missing_asset() -> None:
    # "Atlas could not be searched" must never render as "the asset is absent".
    reads = dict(_both_right())
    reads[_ref("sch_empty")] = Unreadable(cause=RuntimeError("atlas 503"))  # type: ignore[assignment]
    findings = evaluate_attributes_at(reads, _expect())
    assert [f.expectation for f in findings] == [UNREADABLE]
    assert "atlas 503" in findings[0].detail


def test_no_declaration_is_a_noop() -> None:
    assert evaluate_attributes_at(_both_right(), AssetExpectations()) == []


# ---------------------------------------------------------------------------
# Declaration shape
# ---------------------------------------------------------------------------


def test_the_nested_declaration_flattens_to_one_entry_per_asset() -> None:
    normalised = normalise_attribute_expectations_at(_FIVE_SCHEMAS)
    assert normalised == {
        _ref("sch_empty"): {"viewsCount": Exactly(0), "tableCount": Exactly(3)},
        _ref("sch_busy"): {"viewsCount": Exactly(10), "tableCount": Exactly(8)},
    }


def test_matchers_pass_through_the_flattening() -> None:
    normalised = normalise_attribute_expectations_at(
        {"Schema": {"sch": {"viewsCount": Present()}}}
    )
    assert normalised == {_ref("sch"): {"viewsCount": Present()}}


def test_the_same_suffix_under_two_types_is_two_refs() -> None:
    # Which is why the type is part of the key rather than decoration.
    normalised = normalise_attribute_expectations_at(
        {
            "Table": {"orders": {"rowCount": 1}},
            "View": {"orders": {"rowCount": 2}},
        }
    )
    assert set(normalised) == {_ref("orders", "Table"), _ref("orders", "View")}


def test_a_ref_renders_readably_as_a_finding_subject() -> None:
    assert str(_ref("sch_empty")) == "Schema[sch_empty]"
