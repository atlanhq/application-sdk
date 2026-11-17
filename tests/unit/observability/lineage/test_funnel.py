from __future__ import annotations

from application_sdk.observability.lineage import (
    Assessment,
    RefOutcome,
    Verdict,
    settle,
)
from application_sdk.observability.lineage.funnel import (
    EXPECTATION_MISMATCH,
    FUNNEL_BY_CATEGORY,
    LOOKUP_ALL_SOURCES,
    PARSE_ERROR,
    PARTIAL_PARSE,
    funnel_bucket,
)
from application_sdk.observability.lineage.taxonomy import (
    CustomSqlParserFailure,
    NonSqlSource,
    ReasonCategory,
    UpstreamNotInAtlan,
)


def test_every_category_has_a_funnel_entry():
    assert set(FUNNEL_BY_CATEGORY) == set(ReasonCategory)


def test_no_bucket_while_valid_or_pending():
    assert funnel_bucket(Assessment(Verdict.VALID)) is None
    assert funnel_bucket(Assessment(Verdict.PENDING)) is None


def test_expected_none_is_an_expectation_bucket():
    assert (
        funnel_bucket(Assessment(Verdict.VALID_NONE, NonSqlSource))
        == EXPECTATION_MISMATCH
    )


def test_parser_failure_maps_to_parse_error():
    assert (
        funnel_bucket(Assessment(Verdict.MISSING, CustomSqlParserFailure))
        == PARSE_ERROR
    )


def test_lookup_miss_buckets_only_when_nothing_resolved():
    assert (
        funnel_bucket(Assessment(Verdict.MISSING, UpstreamNotInAtlan))
        == LOOKUP_ALL_SOURCES
    )
    assert funnel_bucket(Assessment(Verdict.INVALID, UpstreamNotInAtlan)) is None


def test_reason_can_override_its_category_bucket():
    class SomeQueriesNotParsed(CustomSqlParserFailure):
        code = "TEST_SOME_QUERIES_NOT_PARSED"
        funnel = PARTIAL_PARSE

    assert (
        funnel_bucket(Assessment(Verdict.INVALID, SomeQueriesNotParsed))
        == PARTIAL_PARSE
    )


def test_settle_ignores_refs_not_expected_to_resolve():
    pending = Assessment(Verdict.PENDING)
    mixed = [RefOutcome.MATCHED, RefOutcome.NOT_EXPECTED, RefOutcome.NOT_EXPECTED]
    assert settle(pending, mixed).verdict is Verdict.VALID


def test_settle_with_only_not_expected_refs_expects_no_lineage():
    settled = settle(Assessment(Verdict.PENDING), [RefOutcome.NOT_EXPECTED])
    assert settled.verdict is Verdict.VALID_NONE
    assert settled.reason is NonSqlSource


def test_settle_still_waits_for_unreported_refs():
    pending = Assessment(Verdict.PENDING)
    assert settle(pending, [RefOutcome.NOT_EXPECTED, None]).verdict is Verdict.PENDING
