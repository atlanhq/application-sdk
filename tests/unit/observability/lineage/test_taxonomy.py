"""Lineage coverage taxonomy: closed sets, leaves, and how an app extends them."""

from __future__ import annotations

import logging

import pytest

from application_sdk.observability.lineage import (
    SDK_REASONS,
    CustomSqlParserFailure,
    GapCategory,
    LineageReason,
    NonSqlSource,
    NoUpstreamTables,
    ReasonCategory,
    ReasonRegistry,
    UninstrumentedPath,
    Verdict,
)


class DatasourceHasNoUpstreamTables(NoUpstreamTables):
    """An app-specific reason, shaped like the Tableau app's."""

    code = "DATASOURCE_HAS_NO_UPSTREAM_TABLES"
    customer_message = "Check the crawler user can see the connections."


def test_one_sdk_leaf_per_category():
    leaves = {r.category for r in SDK_REASONS if r.code == r.category.value}
    assert leaves == set(ReasonCategory)


def test_category_set_frozen_at_twelve():
    assert len(ReasonCategory) == 12


def test_sdk_codes_are_unique():
    codes = [r.code for r in SDK_REASONS]
    assert len(codes) == len(set(codes))


def test_app_subclass_inherits_category_and_gap():
    reason = DatasourceHasNoUpstreamTables
    assert reason.category is ReasonCategory.NO_UPSTREAM_DATA
    assert reason.gap is GapCategory.SOURCE
    assert reason.verdict() is Verdict.MISSING


def test_expected_reason_is_valid_none():
    assert NonSqlSource.verdict() is Verdict.VALID_NONE
    assert NonSqlSource.customer_message


def test_subclass_must_declare_its_own_code():
    with pytest.raises(TypeError, match="own UPPER_SNAKE"):

        class NoCode(CustomSqlParserFailure):
            pass


def test_code_must_be_upper_snake():
    with pytest.raises(TypeError, match="UPPER_SNAKE"):

        class Lower(CustomSqlParserFailure):
            code = "custom_sql_broke"


def test_gap_must_be_a_gap_category():
    with pytest.raises(TypeError, match="GapCategory"):

        class BadGap(CustomSqlParserFailure):
            code = "BAD_GAP"
            gap = "PROCESSING"  # type: ignore[assignment]


def test_reasons_are_never_instantiated():
    with pytest.raises(TypeError, match="record the class"):
        CustomSqlParserFailure()


def test_registry_includes_sdk_and_app_reasons():
    registry = ReasonRegistry([DatasourceHasNoUpstreamTables])
    assert len(registry) == len(SDK_REASONS) + 1
    assert registry.resolve("CUSTOM_SQL_PARSER_FAILURE") is CustomSqlParserFailure
    assert (
        registry.resolve("DATASOURCE_HAS_NO_UPSTREAM_TABLES")
        is DatasourceHasNoUpstreamTables
    )


def test_registry_rejects_a_code_declared_twice():
    class Clash(LineageReason):
        code = "CUSTOM_SQL_PARSER_FAILURE"
        category = ReasonCategory.PARSER_FAILURE
        gap = GapCategory.PROCESSING

    with pytest.raises(ValueError, match="declared by both"):
        ReasonRegistry([Clash])


def test_app_restates_an_sdk_reason_by_subclassing_it():
    class OwnWording(CustomSqlParserFailure):
        code = "CUSTOM_SQL_PARSER_FAILURE"
        customer_message = "Atlan could not parse this datasource's custom SQL."

    registry = ReasonRegistry([OwnWording])
    assert registry.resolve("CUSTOM_SQL_PARSER_FAILURE") is OwnWording
    assert len(registry) == len(SDK_REASONS)


def test_a_restated_reason_keeps_its_category():
    class Moved(CustomSqlParserFailure):
        code = "CUSTOM_SQL_PARSER_FAILURE"
        category = ReasonCategory.MISSING_METADATA

    with pytest.raises(ValueError, match="declared by both"):
        ReasonRegistry([Moved])


def test_two_app_restatements_of_one_code_clash():
    class First(CustomSqlParserFailure):
        code = "CUSTOM_SQL_PARSER_FAILURE"

    class Second(CustomSqlParserFailure):
        code = "CUSTOM_SQL_PARSER_FAILURE"

    with pytest.raises(ValueError, match="declared by both"):
        ReasonRegistry([First, Second])


def test_registry_rejects_non_reasons():
    with pytest.raises(TypeError):
        ReasonRegistry(["CUSTOM_SQL_PARSER_FAILURE"])  # type: ignore[list-item]


def test_unknown_code_is_uninstrumented_and_warns_once(caplog):
    registry = ReasonRegistry()
    with caplog.at_level(logging.WARNING):
        assert registry.resolve("NOT_A_CODE") is UninstrumentedPath
        assert registry.resolve("NOT_A_CODE") is UninstrumentedPath
    assert registry.resolve(None) is UninstrumentedPath
