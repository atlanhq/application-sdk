"""Unit tests for the extracted asset-expectation evaluators.

The behaviour these cover already had 19 tests against
``BaseE2ETest._evaluate_asset_expectations`` / ``._validate_asset_locations``,
which stay exactly as they are — the class is not rewired until child H, and a
green run of both suites is what says the extraction preserved the logic.

What is new here is the part that could not be tested on the class: an
:class:`Unreadable` reading. On the class a failed search is spelled ``[]`` or
``0``, which is indistinguishable from a successful search that found nothing,
so there was no input that could express it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from application_sdk.testing.harness.expectations import (
    UNREADABLE,
    AssetExpectations,
    Unreadable,
    evaluate_counts,
    evaluate_locations,
)
from application_sdk.testing.harness.outcome import (
    Indeterminate,
    Settled,
    as_count,
    as_counts,
    as_samples,
)

_SQL_DEPTHS = {"Database": 1, "Schema": 2, "Table": 3, "View": 3, "Column": 4}


def _sql_locations() -> AssetExpectations:
    """Location expectations for the db > schema > table > column shape."""
    return AssetExpectations(
        depths=_SQL_DEPTHS,
        connection_qualified_name="default/x/123",
    )


# ---------------------------------------------------------------------------
# Counts: floors
# ---------------------------------------------------------------------------


def test_floors_met_passes() -> None:
    expectations = AssetExpectations(floors={"Database": 1, "Table": 5})
    assert evaluate_counts({"Database": 1, "Table": 7}, expectations) == []


def test_floors_shortfall_names_the_type_and_the_floor() -> None:
    expectations = AssetExpectations(floors={"Database": 1, "Table": 5})
    findings = evaluate_counts({"Database": 1, "Table": 2}, expectations)
    assert [(f.subject, f.expectation) for f in findings] == [("Table", "floor")]
    assert findings[0].detail == "got 2, expected >= 5"


def test_a_type_absent_from_the_counts_reads_as_zero() -> None:
    """ "The search ran and found none" is what an absent key means."""
    findings = evaluate_counts({}, AssetExpectations(floors={"Table": 1}))
    assert findings[0].detail == "got 0, expected >= 1"


# ---------------------------------------------------------------------------
# Counts: exact parity
# ---------------------------------------------------------------------------


def test_exact_counts_match_passes() -> None:
    expectations = AssetExpectations(exacts={"Database": 1, "Schema": 3})
    assert evaluate_counts({"Database": 1, "Schema": 3}, expectations) == []


def test_exact_catches_over_and_under_extraction() -> None:
    expectations = AssetExpectations(exacts={"Schema": 3})
    over = evaluate_counts({"Schema": 5}, expectations)
    under = evaluate_counts({"Schema": 1}, expectations)
    assert [f.expectation for f in over] == ["exact"]
    assert [f.expectation for f in under] == ["exact"]
    assert "expected exactly 3" in over[0].detail
    assert "expected exactly 3" in under[0].detail


def test_floors_and_exacts_accumulate_rather_than_shadow() -> None:
    expectations = AssetExpectations(
        floors={"Database": 1, "Table": 5}, exacts={"Schema": 3}
    )
    findings = evaluate_counts({"Database": 1, "Table": 2, "Schema": 5}, expectations)
    assert [(f.subject, f.expectation) for f in findings] == [
        ("Table", "floor"),
        ("Schema", "exact"),
    ]


# ---------------------------------------------------------------------------
# Counts: the non-empty backstop
# ---------------------------------------------------------------------------


def test_nonempty_fires_for_a_connector_that_declared_nothing() -> None:
    """The population most likely to regress silently."""
    findings = evaluate_counts({}, AssetExpectations(), total_assets=0)
    assert [f.expectation for f in findings] == ["nonempty"]
    assert "ZERO assets" in findings[0].detail


def test_nonempty_reads_the_all_types_total_not_the_probed_dict() -> None:
    assert evaluate_counts({}, AssetExpectations(), total_assets=5) == []


def test_nonempty_opt_out_leaves_only_the_floor_finding() -> None:
    expectations = AssetExpectations(floors={"Database": 1}, require_nonempty=False)
    findings = evaluate_counts({}, expectations)
    assert [f.expectation for f in findings] == ["floor"]


def test_an_expectation_asserting_zero_is_not_overridden_by_the_backstop() -> None:
    """ "Produces exactly 0 of X" plus zero assets has to pass."""
    expectations = AssetExpectations(exacts={"PlaceholderType": 0})
    assert evaluate_counts({}, expectations, total_assets=0) == []


def test_the_backstop_falls_back_to_the_sum_when_no_total_is_given() -> None:
    expectations = AssetExpectations(floors={"Table": 1})
    assert evaluate_counts({"Table": 3}, expectations) == []


# ---------------------------------------------------------------------------
# Counts: unreadable — the fail-open closure (C4 on FND-224)
# ---------------------------------------------------------------------------


def test_an_unreadable_count_is_not_reported_as_a_missed_floor() -> None:
    """The old shape read a failed search as 0 and blamed the connector for a
    floor it was never graded against."""
    findings = evaluate_counts(
        {"Table": Unreadable(cause=TimeoutError("atlas 503"))},
        AssetExpectations(floors={"Table": 5}),
        total_assets=9,
    )
    assert [(f.subject, f.expectation) for f in findings] == [("Table", UNREADABLE)]
    assert "floor expectation was not graded" in findings[0].detail
    assert "TimeoutError: atlas 503" in findings[0].detail


def test_an_unreadable_count_is_not_reported_as_an_exact_mismatch() -> None:
    findings = evaluate_counts(
        {"Schema": Unreadable(cause=ConnectionError("reset"))},
        AssetExpectations(exacts={"Schema": 3}),
        total_assets=9,
    )
    assert [f.expectation for f in findings] == [UNREADABLE]
    assert "exact expectation was not graded" in findings[0].detail


def test_one_failed_read_ungrades_every_check_that_consulted_it() -> None:
    """Two findings for one cause is the honest count: the floor was not graded,
    and neither was the backstop, because the fallback total cannot be summed."""
    findings = evaluate_counts(
        {"Table": Unreadable(cause=TimeoutError("atlas 503"))},
        AssetExpectations(floors={"Table": 5}),
    )
    assert [(f.subject, f.expectation) for f in findings] == [
        ("Table", UNREADABLE),
        ("all asset types", UNREADABLE),
    ]


def test_an_unreadable_total_does_not_read_as_zero_assets() -> None:
    findings = evaluate_counts(
        {},
        AssetExpectations(),
        total_assets=Unreadable(cause=TimeoutError("atlas 503")),
    )
    assert [f.expectation for f in findings] == [UNREADABLE]
    assert "ZERO assets" not in findings[0].detail


def test_the_sum_fallback_refuses_to_add_around_an_unreadable_count() -> None:
    """A partial sum is low by an unknown amount, and it is compared against
    zero — so "low" is exactly the direction that manufactures a regression."""
    findings = evaluate_counts(
        {"Table": Unreadable(cause=TimeoutError("atlas 503"))},
        AssetExpectations(),
    )
    assert [f.expectation for f in findings] == [UNREADABLE]


def test_an_unreadable_count_for_an_undeclared_type_is_ignored() -> None:
    """Nothing consulted it, so nothing went ungraded."""
    findings = evaluate_counts(
        {"Table": 1, "Ignored": Unreadable(cause=TimeoutError("x"))},
        AssetExpectations(floors={"Table": 1}),
        total_assets=1,
    )
    assert findings == []


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def test_correct_hierarchy_passes() -> None:
    samples = {
        "Database": ["default/x/123/db"],
        "Schema": ["default/x/123/db/sch"],
        "Table": ["default/x/123/db/sch/tbl", "default/x/123/db/sch/tbl2"],
        "View": ["default/x/123/db/sch/vw"],
        "Column": ["default/x/123/db/sch/tbl/col"],
    }
    assert evaluate_locations(samples, _sql_locations()) == []


def test_wrong_depth_names_the_type_and_the_expected_depth() -> None:
    # Table is missing the schema segment -> 2 below the connection, expected 3.
    findings = evaluate_locations({"Table": ["default/x/123/db/tbl"]}, _sql_locations())
    assert [(f.subject, f.expectation) for f in findings] == [("Table", "depth")]
    assert "expected 3" in findings[0].detail


def test_an_asset_outside_the_connection_is_a_nesting_finding() -> None:
    # Right depth, wrong connection prefix (app-name / epoch drift).
    findings = evaluate_locations({"Database": ["default/y/999/db"]}, _sql_locations())
    assert [f.expectation for f in findings] == ["nesting"]
    assert "not nested under the connection default/x/123" in findings[0].detail


def test_a_type_with_no_samples_is_skipped() -> None:
    """ "Too few or none" is the count floors' job, not this check's."""
    samples = {"Database": ["default/x/123/db"], "Table": []}
    assert evaluate_locations(samples, _sql_locations()) == []


def test_a_trailing_slash_does_not_overcount_the_depth() -> None:
    assert (
        evaluate_locations({"Database": ["default/x/123/db/"]}, _sql_locations()) == []
    )


def test_declaring_no_depths_is_a_noop() -> None:
    expectations = AssetExpectations(connection_qualified_name="default/x/123")
    assert evaluate_locations({"Table": ["totally/wrong/place"]}, expectations) == []


def test_every_bad_sample_is_reported_not_just_the_first() -> None:
    samples = {
        "Schema": ["default/x/123/db/sch", "default/x/123/sch_flat"],  # 2nd is depth 1
        "Column": ["default/x/123/db/sch/tbl/col/extra"],  # depth 5
    }
    findings = evaluate_locations(samples, _sql_locations())
    assert [(f.subject, f.expectation) for f in findings] == [
        ("Schema", "depth"),
        ("Column", "depth"),
    ]


def test_no_connection_prefix_skips_the_whole_check() -> None:
    """Depth is measured below the prefix, so without one there is nothing to
    measure from — and asserting nesting under "" would fail every sample."""
    expectations = AssetExpectations(depths=_SQL_DEPTHS)
    assert (
        evaluate_locations({"Table": ["default/x/123/db/sch/tbl"]}, expectations) == []
    )


def test_an_unreadable_sample_can_no_longer_be_spelled_as_no_samples() -> None:
    """The fail-open path this extraction closes: the sampling read returned []
    on any search error, which arrived as "no samples, skip" — a silent pass."""
    findings = evaluate_locations(
        {"Table": Unreadable(cause=PermissionError("403 from search"))},
        _sql_locations(),
    )
    assert [(f.subject, f.expectation) for f in findings] == [("Table", UNREADABLE)]
    assert "PermissionError: 403 from search" in findings[0].detail


def test_a_readable_type_is_still_graded_when_a_sibling_read_failed() -> None:
    samples = {
        "Table": Unreadable(cause=PermissionError("403")),
        "Schema": ["default/x/123/sch_flat"],
    }
    findings = evaluate_locations(samples, _sql_locations())
    assert [(f.subject, f.expectation) for f in findings] == [
        ("Schema", "depth"),
        ("Table", UNREADABLE),
    ]


# ---------------------------------------------------------------------------
# Finding shape
# ---------------------------------------------------------------------------


def test_findings_are_immutable_and_carry_a_machine_readable_expectation() -> None:
    """The evidence bundle groups by what a finding is about, so the report must
    not have to re-parse the prose to find out."""
    findings = evaluate_counts({"Table": 0}, AssetExpectations(floors={"Table": 1}))
    finding = findings[0]
    assert (finding.subject, finding.expectation) == ("Table", "floor")
    assert finding.detail
    with pytest.raises((AttributeError, TypeError)):
        # misc: the write is the assertion — Finding is frozen, and the point is
        # that the type checker and the runtime agree about that.
        finding.subject = "Schema"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The projection from a reader's verdict into an evaluator's reading
# ---------------------------------------------------------------------------
#
# The seam that makes "could not read" survive the trip from
# ``application_sdk.testing.harness.atlas`` to :func:`evaluate_counts`. Each of
# these has a mirror-image bug behind it: before the projection existed an
# unreadable batch arrived as zeros and was graded as a low count, and an
# unreadable sample arrived as ``[]`` and was skipped.


def _settled(value: object) -> Settled[Any]:
    return Settled(label="read", attempts=1, elapsed=timedelta(0), value=value)


def _indeterminate(cause: BaseException) -> Indeterminate[Any]:
    return Indeterminate(label="read", attempts=1, elapsed=timedelta(0), cause=cause)


def test_a_settled_count_projects_to_the_number() -> None:
    assert as_count(_settled(7)) == 7


def test_an_unreadable_count_keeps_its_cause() -> None:
    boom = RuntimeError("atlas is down")
    read = as_count(_indeterminate(boom))
    assert isinstance(read, Unreadable)
    assert read.cause is boom


def test_an_unreadable_batch_marks_every_type_that_was_asked_for() -> None:
    """A type simply *missing* from the mapping counts as zero — the fail-open."""
    reads = as_counts(_indeterminate(RuntimeError("down")), ("Table", "Column"))
    assert set(reads) == {"Table", "Column"}
    assert all(isinstance(value, Unreadable) for value in reads.values())


def test_a_settled_batch_projects_the_mapping_through() -> None:
    reads = as_counts(_settled({"Table": 3, "Column": 0}), ("Table", "Column"))
    assert reads == {"Table": 3, "Column": 0}


def test_an_unreadable_sample_is_not_an_empty_one() -> None:
    """The distinction that stops a failed search reading as a silent pass."""
    reads = as_samples(_indeterminate(RuntimeError("down")), ("Table",))
    assert isinstance(reads["Table"], Unreadable)


def test_a_settled_sample_projects_to_a_list() -> None:
    reads = as_samples(_settled({"Table": ("a", "b")}), ("Table",))
    assert reads == {"Table": ["a", "b"]}


def test_a_projected_unreadable_batch_grades_as_ungraded_not_unmet() -> None:
    """End to end: never "floor not met" for a search nobody could read."""
    reads = as_counts(_indeterminate(RuntimeError("down")), ("Table",))
    findings = evaluate_counts(reads, AssetExpectations(floors={"Table": 5}))
    assert findings
    assert {finding.expectation for finding in findings} == {UNREADABLE}
