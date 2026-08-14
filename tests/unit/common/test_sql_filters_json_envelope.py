"""A ``{...}`` filter string that fails to parse is malformed, not an injection.

``validate_filter_no_sql_injection`` parses ``{...}``-shaped strings before
applying the deny-list, so a legacy filter-as-JSON-string isn't rejected for the
double-quotes JSON syntax requires. That escape hatch only covered *parseable*
JSON: on a decode failure the raw JSON text fell through to the deny-list, which
always found ``"`` and reported a SQL-injection attempt on a character the
caller never authored.

These tests pin the *verdict*, not acceptance — a malformed filter is still
rejected, just for the right reason.
"""

from __future__ import annotations

from collections.abc import Callable

import pydantic
import pytest

from application_sdk.common.sql_filters import (
    prepare_filters,
    validate_filter_no_sql_injection,
)
from application_sdk.templates.contracts.sql_metadata import ExtractionInput

PRODUCTION_INCLUDE_FILTER = r'{"^d\_edw\_stg\_03$": []}'

ACCEPTED = "accepted"
UNSAFE = "rejected_as_unsafe"
MALFORMED = "rejected_as_malformed"


def _verdict(call: Callable[[], object]) -> str:
    """Classify *call*'s outcome, letting an unexpected exception propagate."""
    try:
        call()
    except ValueError as exc:
        if "SQL-unsafe sequence" in str(exc):
            return UNSAFE
        if "Malformed JSON" in str(exc):
            return MALFORMED
        raise
    return ACCEPTED


class TestUnparseableEnvelopeIsMalformedNotUnsafe:
    @pytest.mark.parametrize(
        "value",
        [
            PRODUCTION_INCLUDE_FILTER,
            r'{"C:\temp\staging$": []}',
            r'{"^stg\uZZZZ$": []}',
            r'{"^stg\_a$": [}',
            '{"^stg_a$": [],}',
            f"  {PRODUCTION_INCLUDE_FILTER}  ",
            # Isolates the double-quote exclusion: a stray quote beyond the
            # envelope's structural ones must not flip the verdict to UNSAFE.
            '{"a": "b"c"}',
        ],
    )
    def test_validator(self, value: str) -> None:
        assert _verdict(lambda: validate_filter_no_sql_injection(value)) == MALFORMED

    @pytest.mark.parametrize(
        "value",
        [
            PRODUCTION_INCLUDE_FILTER,
            r'{"C:\temp\staging$": []}',
            r'{"^stg\uZZZZ$": []}',
        ],
    )
    def test_prepare_filters(self, value: str) -> None:
        # ``prepare_filters`` also runs ``parse_filter_input``, which decodes
        # independently, so the verdict has to hold end-to-end.
        assert _verdict(lambda: prepare_filters(value, "{}")) == MALFORMED

    def test_message_names_the_parse_position(self) -> None:
        # The behaviour under test is that the message CARRIES a position, so the
        # operator reading it can find the offending character. The exact column
        # is the decoder's to choose, not ours: orjson 3.12.0 moved this one from
        # 6 to 5 (it now points at the backslash rather than the character after
        # it), which reddened all twelve unit-test jobs on a lockfile refresh.
        # Pinning the number tested orjson, not us.
        with pytest.raises(ValueError, match=r"Malformed JSON.*line 1 column \d+"):
            validate_filter_no_sql_injection(PRODUCTION_INCLUDE_FILTER)


class TestInjectionStillGetsTheSecurityVerdict:
    @pytest.mark.parametrize(
        "value",
        [
            '{"^prod--$": []}',
            '{"a"; DROP TABLE x": []}',
            r'{"^a\_b$": []} ; DROP TABLE x --}',
            r'{"^a\_b--c$": []}',
            r"{\"^a\_b$\": ['x'] }",
            '{"^a\\_b\x00$": []}',
            r'{"^a\_b/*c$": []}',
        ],
    )
    def test_verdict_is_unsafe(self, value: str) -> None:
        # A stray backslash must not become a way to hide a forbidden sequence
        # from the deny-list.
        assert _verdict(lambda: validate_filter_no_sql_injection(value)) == UNSAFE


class TestUnchangedPaths:
    @pytest.mark.parametrize(
        "value",
        [
            {"^d\\_edw\\_stg\\_03$": []},
            {"^db$": {"^sch$": {}}},
        ],
    )
    def test_dict_paths_pass(self, value: dict) -> None:
        assert validate_filter_no_sql_injection(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            '{"^d\\\\_edw$": []}',
            # Brace-wrapped but quote-free: never a JSON attempt, so it keeps
            # raw-regex handling. ``_prepare_sql``'s no-cascade tests rely on
            # exactly this shape.
            "{normalized_include_regex}",
            "{2,3}",
        ],
    )
    def test_string_paths_pass(self, value: str) -> None:
        assert validate_filter_no_sql_injection(value) == value

    def test_correctly_escaped_json_string_still_normalises(self) -> None:
        include, exclude = prepare_filters('{"^d\\\\_edw$": []}', "{}")
        assert include == "^d\\_edw\\..*$"
        assert exclude == "^$"


class TestExtractionInputDecoding:
    def test_malformed_filter_is_collected_by_pydantic(self) -> None:
        # The rejection stays a ValueError so Pydantic collects it into a
        # ValidationError rather than propagating a raw error to every caller.
        with pytest.raises(pydantic.ValidationError) as exc_info:
            ExtractionInput(include_filter=PRODUCTION_INCLUDE_FILTER)
        assert "Malformed JSON" in str(exc_info.value)
        assert "SQL-unsafe sequence" not in str(exc_info.value)

    def test_valid_filter_decodes(self) -> None:
        assert (
            ExtractionInput(include_filter='{"^d_edw_stg_03$": []}').include_filter
            == '{"^d_edw_stg_03$": []}'
        )
