"""SQL injection deny-list — covers raw-dict helper paths (BLDX-518).

The Pydantic-validated paths (``ExtractionInput`` / ``ExtractionTaskInput``)
are covered by ``tests/unit/templates/test_extraction_input_filters.py``.
This file pins the helper-level enforcement: ``prepare_query``,
``prepare_filters``, and ``get_database_names`` all run untyped
``workflow_args`` dicts through the same deny-list before any SQL
template substitution.

Why this matters: connector apps call these helpers directly with raw
``metadata`` payloads sourced from workflow input, which bypasses every
Pydantic guard upstream. A regression here would re-open the original
BLDX-518 vector even with the Pydantic validators in place.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from application_sdk.common.sql_filters import (
    get_database_names,
    prepare_filters,
    prepare_query,
    validate_filter_no_sql_injection,
)

# ---------------------------------------------------------------------------
# validate_filter_no_sql_injection — the deny-list itself
# ---------------------------------------------------------------------------


class TestValidateFilterNoSqlInjection:
    @pytest.mark.parametrize(
        "value",
        [
            "prefix'injection",  # single quote
            'prefix"injection',  # double quote
            "name; DROP TABLE x",  # statement separator
            "name-- comment",  # SQL line comment
            "name/* block",  # block comment open
            "name */",  # block comment close
            "name\x00trailing",  # null byte
        ],
    )
    def test_string_with_forbidden_sequence_raises(self, value: str) -> None:
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            validate_filter_no_sql_injection(value)

    @pytest.mark.parametrize(
        "value",
        [
            "^prod_.*$",  # simple regex
            r"^(prod|stage)_db\.[a-z]+(\.bak)?$",  # full meta-char set
            "",  # empty
            "schema_name",  # plain identifier
        ],
    )
    def test_clean_string_passes(self, value: str) -> None:
        assert validate_filter_no_sql_injection(value) == value

    def test_dict_key_with_quote_raises(self) -> None:
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            validate_filter_no_sql_injection({"db'; DROP--": ["^public$"]})

    def test_dict_value_with_quote_raises(self) -> None:
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            validate_filter_no_sql_injection({"^db$": ["sch'; DROP--"]})

    def test_dict_value_as_string_with_quote_raises(self) -> None:
        # Some callers pass a string value instead of a list.
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            validate_filter_no_sql_injection({"^db$": "sch';--"})

    def test_clean_dict_passes(self) -> None:
        v = {"^prod$": ["^analytics$", "^reporting$"]}
        assert validate_filter_no_sql_injection(v) == v

    def test_json_encoded_string_parses_and_validates(self) -> None:
        # Legacy AE payload shape: filter encoded as JSON string. Double
        # quotes belong to JSON syntax, not filter content — must pass.
        v = '{"^prod$": ["^analytics$"]}'
        assert validate_filter_no_sql_injection(v) == v

    def test_json_encoded_string_with_injected_quote_raises(self) -> None:
        # The JSON structural quotes are fine; the single quote inside
        # the actual filter value still fails.
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            validate_filter_no_sql_injection('{"^prod$": ["sch\'; DROP TABLE x"]}')

    def test_malformed_json_falls_through_to_string_check(self) -> None:
        # Not valid JSON despite the braces — treated as raw regex; the
        # single quote in the value still trips the deny-list.
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            validate_filter_no_sql_injection("{ not really json ' }")


# ---------------------------------------------------------------------------
# prepare_filters — string/dict inputs are gated before normalization
# ---------------------------------------------------------------------------


class TestPrepareFiltersInjectionGate:
    def test_quote_in_include_raises(self) -> None:
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            prepare_filters("prefix'injection", "")

    def test_quote_in_exclude_raises(self) -> None:
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            prepare_filters("", "name; DROP TABLE x")

    def test_clean_filters_pass(self) -> None:
        # ``prepare_filters`` calls ``parse_filter_input`` which JSON-decodes
        # the string, so the input must be a JSON-encoded filter map (the
        # shape AE actually emits). The deny-list runs on the parsed dict
        # contents, not the JSON syntax.
        include, exclude = prepare_filters(
            '{"^prod$": ["^analytics$"]}',
            '{"^temp$": ["^staging$"]}',
        )
        assert include
        assert exclude


# ---------------------------------------------------------------------------
# prepare_query — workflow_args dict is gated for temp-table-regex
# ---------------------------------------------------------------------------


class TestPrepareQueryInjectionGate:
    def test_quote_in_temp_table_regex_raises(self) -> None:
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            prepare_query(
                "SELECT 1",
                {"metadata": {"temp-table-regex": "evil'; DROP TABLE--"}},
                temp_table_regex_sql="AND name NOT LIKE '{exclude_table_regex}'",
            )

    def test_quote_in_include_filter_raises(self) -> None:
        # ``prepare_query`` delegates filter validation to ``prepare_filters``;
        # the injection guard fires there.
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            prepare_query(
                "SELECT 1",
                {"metadata": {"include-filter": "name'; DROP TABLE x"}},
            )


# ---------------------------------------------------------------------------
# get_database_names — include-filter is gated before any return path
# ---------------------------------------------------------------------------


class TestGetDatabaseNamesInjectionGate:
    async def test_quote_in_include_filter_raises(self) -> None:
        sql_client = MagicMock()
        sql_client.get_results = AsyncMock()
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            await get_database_names(
                sql_client,
                {"metadata": {"include-filter": {"db'; DROP--": ["public"]}}},
                "SELECT name FROM information_schema.databases",
            )
        # SQL was never reached.
        sql_client.get_results.assert_not_called()
