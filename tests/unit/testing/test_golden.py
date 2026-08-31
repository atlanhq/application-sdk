"""Unit tests for the shared golden-diff assertion."""

import pytest

from application_sdk.testing.golden import (
    DiffPolicy,
    GoldenReport,
    TypenameRule,
    assert_matches_golden,
    diff_golden,
)


def asset(qualified_name: str, typename: str = "Table", **attrs):
    """Build a synthetic asset record."""
    return {
        "typeName": typename,
        "attributes": {"qualifiedName": qualified_name, **attrs},
    }


class TestDiffGolden:
    def test_identical_input_has_no_diffs(self):
        records = [asset("default/db/s/t1"), asset("default/db/s/t2")]
        report = diff_golden(records, records)
        assert not report.has_failures
        assert len(report.diffs) == 1
        assert not report.diffs[0].has_diffs

    def test_extra_in_ours_detected(self):
        report = diff_golden(
            [asset("default/db/s/t1"), asset("default/db/s/t2")],
            [asset("default/db/s/t1")],
        )
        diff = report.diffs[0]
        assert diff.extra_in_ours == ("default/db/s/t2",)
        assert diff.missing_in_ours == ()

    def test_missing_in_ours_detected(self):
        report = diff_golden(
            [asset("default/db/s/t1")],
            [asset("default/db/s/t1"), asset("default/db/s/t2")],
        )
        diff = report.diffs[0]
        assert diff.missing_in_ours == ("default/db/s/t2",)
        assert diff.extra_in_ours == ()

    def test_field_mismatch_reports_path_and_both_values(self):
        report = diff_golden(
            [asset("default/db/s/t1", rowCount=10)],
            [asset("default/db/s/t1", rowCount=5)],
        )
        (mismatch,) = report.diffs[0].mismatches
        assert mismatch.key == "default/db/s/t1"
        (field_diff,) = mismatch.field_diffs
        assert field_diff.field_path == "attributes.rowCount"
        assert field_diff.baseline_value == 5
        assert field_diff.candidate_value == 10

    def test_run_volatile_fields_stripped_by_default(self):
        report = diff_golden(
            [asset("default/db/s/t1", lastSyncRun="run-b", lastSyncRunAt=222)],
            [asset("default/db/s/t1", lastSyncRun="run-a", lastSyncRunAt=111)],
        )
        assert not report.diffs[0].has_diffs

    def test_volatile_fields_stripped_at_every_depth(self):
        report = diff_golden(
            [asset("default/db/s/t1", parent={"lastSyncRun": "run-b", "name": "s"})],
            [asset("default/db/s/t1", parent={"lastSyncRun": "run-a", "name": "s"})],
        )
        assert not report.diffs[0].has_diffs

    def test_environment_scoped_fields_are_kept_not_ignored(self):
        """The FND-819 decision: a differing connectionName is a real diff here."""
        report = diff_golden(
            [asset("default/db/s/t1", connectionName="conn-b")],
            [asset("default/db/s/t1", connectionName="conn-a")],
        )
        assert report.diffs[0].mismatches
        assert report.has_failures

    def test_records_grouped_per_typename(self):
        report = diff_golden(
            [asset("q1", typename="Table"), asset("q2", typename="Column")],
            [asset("q1", typename="Table"), asset("q2", typename="Column")],
        )
        assert [d.typename for d in report.diffs] == ["Column", "Table"]

    def test_records_without_a_key_are_skipped(self):
        report = diff_golden([{"typeName": "Table", "attributes": {}}], [])
        assert report.diffs == ()

    def test_typename_absent_from_one_side_still_reported(self):
        report = diff_golden([asset("q1", typename="Column")], [])
        assert report.diffs[0].typename == "Column"
        assert report.diffs[0].golden_count == 0

    def test_custom_key_callable(self):
        produced = [{"typeName": "Report", "id": "r1", "attributes": {}}]
        golden = [{"typeName": "Report", "id": "r1", "attributes": {}}]
        report = diff_golden(produced, golden, key=lambda r: r.get("id", ""))
        assert not report.diffs[0].has_diffs

    def test_missing_typename_grouped_as_unknown(self):
        report = diff_golden(
            [{"attributes": {"qualifiedName": "q1"}}],
            [{"attributes": {"qualifiedName": "q1"}}],
        )
        assert report.diffs[0].typename == "Unknown"


class TestPolicies:
    def test_strict_gates_on_extras_and_mismatches(self):
        report = diff_golden(
            [asset("q1", rowCount=1), asset("q2")],
            [asset("q1", rowCount=2)],
        )
        diff = report.diffs[0]
        assert diff.rule.policy is DiffPolicy.STRICT
        assert diff.has_failures
        assert len(diff.failures) == 2

    def test_strict_gates_on_missing_by_default(self):
        report = diff_golden([], [asset("q1")])
        assert report.diffs[0].has_failures

    def test_tolerate_missing_suppresses_only_the_missing_reason(self):
        rules = {"Table": TypenameRule(tolerate_missing=True)}
        report = diff_golden([], [asset("q1")], rules=rules)
        assert not report.diffs[0].has_failures
        assert report.diffs[0].missing_in_ours == ("q1",)

    def test_tolerate_missing_still_gates_on_extras(self):
        rules = {"Table": TypenameRule(tolerate_missing=True)}
        report = diff_golden([asset("q1")], [], rules=rules)
        assert report.diffs[0].has_failures

    def test_no_extras_gates_on_extras_only(self):
        rules = {"Table": TypenameRule(policy=DiffPolicy.NO_EXTRAS)}
        report = diff_golden([asset("q1")], [], rules=rules)
        assert report.diffs[0].has_failures

    def test_no_extras_tolerates_mismatches_and_missing(self):
        rules = {"Table": TypenameRule(policy=DiffPolicy.NO_EXTRAS)}
        report = diff_golden(
            [asset("q1", rowCount=1)],
            [asset("q1", rowCount=2), asset("q2")],
            rules=rules,
        )
        diff = report.diffs[0]
        assert diff.mismatches
        assert diff.missing_in_ours == ("q2",)
        assert not diff.has_failures

    def test_info_only_never_gates(self):
        rules = {"Table": TypenameRule(policy=DiffPolicy.INFO_ONLY)}
        report = diff_golden(
            [asset("q1", rowCount=1), asset("q2")],
            [asset("q1", rowCount=2), asset("q3")],
            rules=rules,
        )
        diff = report.diffs[0]
        assert diff.has_diffs
        assert not diff.has_failures

    def test_rules_apply_per_typename_independently(self):
        rules = {
            "Table": TypenameRule(policy=DiffPolicy.STRICT),
            "Column": TypenameRule(policy=DiffPolicy.INFO_ONLY),
        }
        report = diff_golden(
            [asset("q1", typename="Table"), asset("q2", typename="Column")],
            [],
            rules=rules,
        )
        by_type = {d.typename: d for d in report.diffs}
        assert by_type["Table"].has_failures
        assert not by_type["Column"].has_failures

    def test_default_rule_applies_to_unnamed_typenames(self):
        report = diff_golden(
            [asset("q1")],
            [],
            default_rule=TypenameRule(policy=DiffPolicy.INFO_ONLY),
        )
        assert not report.diffs[0].has_failures

    def test_per_typename_ignore_fields_excludes_one_field(self):
        """A field the client computes locally cannot match a sanitized fixture."""
        rules = {"Report": TypenameRule(ignore_fields=frozenset({"source_url"}))}
        report = diff_golden(
            [asset("q1", typename="Report", source_url="https://ours.invalid/r/1")],
            [asset("q1", typename="Report", source_url="https://golden.invalid/r/1")],
            rules=rules,
        )
        assert not report.diffs[0].has_failures

    def test_per_typename_ignore_does_not_weaken_other_fields(self):
        rules = {"Report": TypenameRule(ignore_fields=frozenset({"source_url"}))}
        report = diff_golden(
            [asset("q1", typename="Report", source_url="a", name="ours")],
            [asset("q1", typename="Report", source_url="b", name="golden")],
            rules=rules,
        )
        (mismatch,) = report.diffs[0].mismatches
        assert [fd.field_path for fd in mismatch.field_diffs] == ["attributes.name"]

    def test_per_typename_ignore_does_not_leak_to_other_typenames(self):
        rules = {"Report": TypenameRule(ignore_fields=frozenset({"source_url"}))}
        report = diff_golden(
            [asset("q1", typename="Dossier", source_url="a")],
            [asset("q1", typename="Dossier", source_url="b")],
            rules=rules,
        )
        assert report.diffs[0].has_failures

    def test_explicit_ignore_replaces_the_default_set(self):
        report = diff_golden(
            [asset("q1", lastSyncRun="run-b")],
            [asset("q1", lastSyncRun="run-a")],
            ignore=frozenset(),
        )
        assert report.diffs[0].mismatches

    def test_typename_rule_is_frozen(self):
        rule = TypenameRule()
        with pytest.raises(AttributeError):
            rule.policy = DiffPolicy.INFO_ONLY  # type: ignore[misc]


class TestAssertMatchesGolden:
    def test_passes_on_identical_input(self):
        records = [asset("q1")]
        report = assert_matches_golden(records, records)
        assert isinstance(report, GoldenReport)
        assert not report.has_failures

    def test_raises_assertion_error_on_mismatch(self):
        with pytest.raises(AssertionError) as exc:
            assert_matches_golden(
                [asset("q1", rowCount=1)],
                [asset("q1", rowCount=2)],
            )
        message = str(exc.value)
        assert "Golden comparison FAILED" in message
        assert "attributes.rowCount" in message
        assert "golden=2" in message
        assert "ours=1" in message

    def test_error_message_names_the_failing_typename(self):
        with pytest.raises(AssertionError, match="Column"):
            assert_matches_golden([asset("q1", typename="Column")], [])

    def test_returns_report_on_success_so_ungated_diffs_are_inspectable(self):
        rules = {"Table": TypenameRule(policy=DiffPolicy.INFO_ONLY)}
        report = assert_matches_golden([asset("q1")], [], rules=rules)
        assert report.diffs[0].extra_in_ours == ("q1",)

    def test_does_not_raise_when_only_ungated_typenames_differ(self):
        rules = {"Column": TypenameRule(policy=DiffPolicy.INFO_ONLY)}
        assert_matches_golden(
            [asset("q1", typename="Table"), asset("q2", typename="Column")],
            [asset("q1", typename="Table")],
            rules=rules,
        )


class TestGoldenReportFormatting:
    def test_empty_report(self):
        assert GoldenReport().format_report() == "No typenames compared."

    def test_passing_report_says_passed(self):
        records = [asset("q1")]
        text = diff_golden(records, records).format_report()
        assert "Golden comparison passed." in text

    def test_summary_line_carries_counts_and_policy(self):
        text = diff_golden([asset("q1"), asset("q2")], [asset("q1")]).format_report()
        assert "ours=2 golden=1" in text
        assert "extra=1" in text
        assert "STRICT" in text

    def test_info_only_typename_marked_as_not_gated(self):
        rules = {"Table": TypenameRule(policy=DiffPolicy.INFO_ONLY)}
        text = diff_golden([asset("q1")], [], rules=rules).format_report()
        assert "[info]" in text
        assert "not gated" in text

    def test_key_lists_are_truncated(self):
        produced = [asset(f"q{i}") for i in range(30)]
        text = diff_golden(produced, []).format_report()
        assert "and 20 more" in text

    def test_long_values_are_truncated(self):
        text = diff_golden(
            [asset("q1", note="x" * 500)],
            [asset("q1", note="y")],
        ).format_report()
        assert "..." in text
        assert "x" * 500 not in text

    def test_field_diffs_are_truncated_per_asset(self):
        produced = [asset("q1", **{f"f{i}": i for i in range(20)})]
        golden = [asset("q1", **{f"f{i}": i + 1 for i in range(20)})]
        text = diff_golden(produced, golden).format_report()
        assert "more field(s)" in text

    def test_missing_keys_are_listed(self):
        text = diff_golden([], [asset("q1"), asset("q2")]).format_report()
        assert "missing from ours (2)" in text
        assert "q1" in text

    def test_mismatched_assets_are_truncated(self):
        produced = [asset(f"q{i}", rowCount=1) for i in range(10)]
        golden = [asset(f"q{i}", rowCount=2) for i in range(10)]
        text = diff_golden(produced, golden).format_report()
        assert "more asset(s)" in text


class TestVacuousPass:
    def test_assert_raises_when_nothing_was_compared(self):
        keyless = [{"typeName": "Table", "attributes": {}}]
        with pytest.raises(AssertionError) as exc:
            assert_matches_golden(keyless, keyless)
        message = str(exc.value)
        assert "nothing was compared" in message
        assert "get_qualified_name" in message
        assert "1 produced / 1 golden" in message

    def test_diff_golden_reports_skipped_counts_without_raising(self):
        keyless = [{"typeName": "Table", "attributes": {}}]
        report = diff_golden(keyless, keyless)
        assert report.diffs == ()
        assert report.produced_skipped == 1
        assert report.golden_skipped == 1
        text = report.format_report()
        assert "No typenames compared." in text
        assert "skipped for empty key: ours=1 golden=1" in text

    def test_skipped_counts_surface_in_a_non_empty_report(self):
        report = diff_golden(
            [asset("q1"), {"typeName": "Table", "attributes": {}}],
            [asset("q1")],
        )
        assert report.produced_skipped == 1
        assert "skipped for empty key: ours=1 golden=0" in report.format_report()

    def test_expect_typenames_raises_when_golden_lacks_the_typename(self):
        with pytest.raises(AssertionError, match="missing expected typename"):
            assert_matches_golden(
                [asset("q1", typename="Column")],
                [asset("q1", typename="Column")],
                expect_typenames={"Table"},
            )

    def test_expect_typenames_passes_when_present(self):
        records = [asset("q1", typename="Table")]
        report = assert_matches_golden(records, records, expect_typenames={"Table"})
        assert report.diffs[0].golden_count == 1

    def test_expect_typenames_needs_golden_records_not_just_the_bucket(self):
        with pytest.raises(AssertionError, match="Table"):
            diff_golden(
                [asset("q1", typename="Table")],
                [],
                expect_typenames={"Table"},
            )


class TestDuplicateJoinKeys:
    def test_duplicate_golden_key_raises_value_error_naming_the_key(self):
        with pytest.raises(ValueError, match="golden side") as exc:
            diff_golden([asset("q1")], [asset("q1"), asset("q1", rowCount=2)])
        assert "q1" in str(exc.value)

    def test_duplicate_produced_key_raises_value_error(self):
        with pytest.raises(ValueError, match="produced side"):
            diff_golden([asset("q1"), asset("q1", rowCount=2)], [asset("q1")])

    def test_assert_matches_golden_also_rejects_duplicates_by_default(self):
        with pytest.raises(ValueError):
            assert_matches_golden([asset("q1")], [asset("q1"), asset("q1")])

    def test_last_wins_keeps_old_behaviour_and_reports_the_duplicates(self):
        report = diff_golden(
            [asset("q1", rowCount=2)],
            [asset("q1", rowCount=1), asset("q1", rowCount=2)],
            on_duplicate_key="last-wins",
        )
        diff = report.diffs[0]
        assert diff.golden_count == 1
        assert not diff.has_diffs
        assert diff.duplicate_keys_golden == ("q1",)
        assert diff.duplicate_keys_ours == ()
        assert "duplicate keys in golden for Table (1): q1" in report.format_report()

    def test_last_wins_reports_produced_side_duplicates(self):
        report = diff_golden(
            [asset("q1"), asset("q1")],
            [asset("q1")],
            on_duplicate_key="last-wins",
        )
        assert report.diffs[0].duplicate_keys_ours == ("q1",)

    def test_duplicates_are_per_group_not_global(self):
        report = diff_golden(
            [asset("q1", typename="Table"), asset("q1", typename="Column")],
            [asset("q1", typename="Table"), asset("q1", typename="Column")],
        )
        assert len(report.diffs) == 2


class TestIgnoreSemantics:
    def test_extra_ignore_adds_to_the_canonical_set(self):
        report = diff_golden(
            [asset("q1", myField="a", lastSyncRun="run-b")],
            [asset("q1", myField="b", lastSyncRun="run-a")],
            extra_ignore={"myField"},
        )
        assert not report.diffs[0].has_diffs

    def test_extra_ignore_does_not_weaken_other_fields(self):
        report = diff_golden(
            [asset("q1", myField="a", name="ours")],
            [asset("q1", myField="b", name="golden")],
            extra_ignore={"myField"},
        )
        (mismatch,) = report.diffs[0].mismatches
        assert [fd.field_path for fd in mismatch.field_diffs] == ["attributes.name"]

    def test_empty_ignore_keeps_every_field_including_the_canonical_three(self):
        """Pins the replacement semantics a live consumer relies on."""
        report = diff_golden(
            [asset("q1", lastSyncRun="run-b")],
            [asset("q1", lastSyncRun="run-a")],
            ignore=frozenset(),
            extra_ignore=(),
        )
        assert report.diffs[0].mismatches

    def test_extra_ignore_composes_with_an_explicit_empty_ignore(self):
        report = diff_golden(
            [asset("q1", lastSyncRun="run-b", myField="a")],
            [asset("q1", lastSyncRun="run-a", myField="b")],
            ignore=frozenset(),
            extra_ignore={"myField"},
        )
        (mismatch,) = report.diffs[0].mismatches
        assert [fd.field_path for fd in mismatch.field_diffs] == [
            "attributes.lastSyncRun"
        ]


class TestGroupBy:
    def test_group_by_buckets_records_on_a_custom_discriminator(self):
        produced = [
            {"record_type": "projects", "id": "p1", "name": "ours"},
            {"record_type": "reports", "id": "r1"},
        ]
        golden = [
            {"record_type": "projects", "id": "p1", "name": "golden"},
            {"record_type": "reports", "id": "r1"},
        ]
        report = diff_golden(
            produced,
            golden,
            key=lambda r: r["id"],
            group_by=lambda r: r["record_type"],
            rules={"projects": TypenameRule(policy=DiffPolicy.INFO_ONLY)},
        )
        by_group = {d.typename: d for d in report.diffs}
        assert sorted(by_group) == ["projects", "reports"]
        assert by_group["projects"].mismatches
        assert not by_group["projects"].has_failures
        assert not report.has_failures

    def test_without_group_by_non_atlas_records_all_land_in_unknown(self):
        """Pins the documented default: rules= is inert for this tier."""
        records = [
            {"record_type": "projects", "id": "p1"},
            {"record_type": "reports", "id": "r1"},
        ]
        report = diff_golden(records, records, key=lambda r: r["id"])
        assert [d.typename for d in report.diffs] == ["Unknown"]
        assert report.diffs[0].produced_count == 2

    def test_group_by_overrides_typename_when_both_are_present(self):
        records = [asset("q1", typename="Table")]
        report = diff_golden(records, records, group_by=lambda r: "custom")
        assert report.diffs[0].typename == "custom"


class TestConsumerCallSignatures:
    """Regression pins for the two live consumer call shapes."""

    def test_per_typename_loop_with_default_rule_and_empty_ignore(self):
        produced = [{"typeName": "projects", "id": "p1", "attributes": {}}]
        golden = [{"typeName": "projects", "id": "p1", "attributes": {}}]
        report = assert_matches_golden(
            produced,
            golden,
            key=lambda r: r.get("id", ""),
            default_rule=TypenameRule(policy=DiffPolicy.NO_EXTRAS),
            ignore=frozenset(),
        )
        assert not report.has_failures

    def test_single_bucket_with_a_volatile_key_set(self):
        produced = [{"id": "p1", "name": "n", "loaded_at": 2}]
        golden = [{"id": "p1", "name": "n", "loaded_at": 1}]
        report = assert_matches_golden(
            produced,
            golden,
            key=lambda r: r["id"],
            ignore=frozenset({"loaded_at"}),
        )
        assert not report.has_failures


class TestFalsyTypenameGrouping:
    def test_none_and_missing_typename_share_the_unknown_bucket(self) -> None:
        produced = [
            {"attributes": {"qualifiedName": "a"}, "typeName": None},
            {"attributes": {"qualifiedName": "b"}},
        ]
        golden = [
            {"attributes": {"qualifiedName": "a"}},
            {"attributes": {"qualifiedName": "b"}, "typeName": ""},
        ]
        assert_matches_golden(
            produced,
            golden,
            key=lambda r: r["attributes"]["qualifiedName"],
            ignore=frozenset({"typeName"}),
        )
