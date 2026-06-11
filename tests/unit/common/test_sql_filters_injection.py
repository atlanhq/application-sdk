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
    normalize_legacy_filter_value,
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

    def test_excessive_nested_filter_depth_raises(self) -> None:
        deeply_nested = {"a": {"b": {"c": {"d": {"e": {"f": {}}}}}}}
        with pytest.raises(ValueError, match=r"Filter nesting exceeds maximum depth"):
            validate_filter_no_sql_injection(deeply_nested)

    def test_max_nested_filter_depth_passes(self) -> None:
        at_limit = {"a": {"b": {"c": {"d": {}}}}}
        assert validate_filter_no_sql_injection(at_limit) == at_limit


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


# ---------------------------------------------------------------------------
# normalize_legacy_filter_value — translates pre-v3 quoted-CSV to v3 regex
# alternation. Lets migration callers explicitly opt-in to the conversion
# without weakening the SAFE_FILTER_PATTERN deny-list. (HYP-1560)
# ---------------------------------------------------------------------------


class TestNormalizeLegacyFilterValue:
    @pytest.mark.parametrize(
        ("legacy", "expected"),
        [
            # Two items — the canonical migration shape this helper exists for.
            ('"PREFIX_A.","PREFIX_B."', "PREFIX_A.|PREFIX_B."),
            # Three items.
            ('"x","y","z"', "x|y|z"),
            # Single quoted item.
            ('"only"', "only"),
            # Items that look like proper regex fragments survive intact.
            ('"^foo$","^bar$"', "^foo$|^bar$"),
            # Stray empty item collapses cleanly so we don't emit an
            # alternation branch matching the empty string.
            ('"A",""', "A"),
        ],
    )
    def test_legacy_shape_is_rewritten(self, legacy: str, expected: str) -> None:
        assert normalize_legacy_filter_value(legacy) == expected

    @pytest.mark.parametrize(
        "value",
        [
            # Already-valid v3 single-regex values pass through unchanged.
            "^prod_.*$",
            r"^(prod|stage)_db\.[a-z]+$",
            "PREFIX_A.|PREFIX_B.",
            "schema_name",
            "",
            # Malformed legacy-ish shapes don't accidentally match.
            '"unterminated',
            'no-leading-quote"',
            '"A",unquoted',
        ],
    )
    def test_non_legacy_shape_passes_through(self, value: str) -> None:
        assert normalize_legacy_filter_value(value) == value

    def test_all_empty_items_returns_original(self) -> None:
        # A purely empty payload like ``""`` has no meaningful items to
        # promote; we return it as-is so the caller's validator surfaces
        # the SQL-unsafe quote it actually is.
        assert normalize_legacy_filter_value('""') == '""'

    @pytest.mark.parametrize(
        "legacy_with_forbidden_item",
        [
            # Items themselves carry deny-listed sequences. Even though
            # the wrapping shape is the legacy CSV, the unwrapped result
            # is still SQL-unsafe and must fail loudly rather than
            # silently round-trip through the helper.
            '"name;drop"',
            '"a--b","c"',
            '"a","b/*c"',
            '"a","b\x00c"',
        ],
    )
    def test_forbidden_item_after_unwrap_raises(
        self, legacy_with_forbidden_item: str
    ) -> None:
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            normalize_legacy_filter_value(legacy_with_forbidden_item)

    @pytest.mark.parametrize("value", [None, 42, ["A", "B"], {"A": "B"}])
    def test_non_string_input_passes_through(self, value: object) -> None:
        # The helper is string-typed but defensively returns non-strings
        # untouched so a caller threading dicts through doesn't blow up
        # before reaching its own type guard.
        assert normalize_legacy_filter_value(value) is value  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Raw-dict entry points must auto-normalise the legacy quoted-CSV shape
# before the deny-list runs (HYP-1560). These callers bypass the typed
# ``ExtractionInput`` BeforeValidator, so they each need their own
# normalise-then-validate sequence to keep migrated workflow specs from
# tripping the v3.13 SDK's stricter pattern.
# ---------------------------------------------------------------------------


class TestRawEntryPointLegacyNormalisation:
    def test_prepare_query_temp_table_regex_legacy_is_normalised(self) -> None:
        # Migrated workflow spec carrying the pre-v3 quoted-CSV shape
        # for temp-table-regex now substitutes into the SQL template
        # as a valid alternation regex rather than raising.
        result = prepare_query(
            "SELECT 1 WHERE 1=1 {temp_table_regex_sql}",
            {"metadata": {"temp-table-regex": '"PREFIX_A.","PREFIX_B."'}},
            temp_table_regex_sql="AND name !~ '{exclude_table_regex}'",
        )
        # Expect the alternation regex inside the SQL literal — proves
        # the value was rewritten BEFORE substitution, not after.
        assert result is not None
        assert "PREFIX_A.|PREFIX_B." in result
        # And the original quoted-CSV form is gone — no quote or comma
        # survived into the substituted SQL.
        assert '","' not in result

    def test_prepare_filters_include_legacy_does_not_trip_deny_list(self) -> None:
        # The legacy quoted-CSV normalises to ``A|B`` before the deny-list
        # runs, so the SQL-unsafe ``ValueError`` is NOT raised. A downstream
        # ``parse_filter_input`` JSON decode may still fail on the
        # normalised raw-regex shape — that's the pre-existing behaviour
        # for raw-string filters and orthogonal to this PR. The contract
        # this test pins: the legacy shape no longer fails the deny-list.
        with pytest.raises(Exception) as exc_info:
            prepare_filters('"A","B"', '{"^temp$": ["^staging$"]}')
        assert "SQL-unsafe sequence" not in str(exc_info.value)

    async def test_get_database_names_legacy_string_include_does_not_trip_deny_list(
        self,
    ) -> None:
        # Same property as prepare_filters: the string-typed include-filter
        # normalises before the deny-list runs.
        sql_client = MagicMock()
        sql_client.get_results = AsyncMock(return_value={"database_name": ["x"]})

        with pytest.raises(Exception) as exc_info:
            await get_database_names(
                sql_client,
                {"metadata": {"include-filter": '"db_a","db_b"'}},
                "SELECT name FROM information_schema.databases",
            )
        assert "SQL-unsafe sequence" not in str(exc_info.value)
