"""Tests for ExtractionInput filter value handling (BLDX-1125).

Filters accept dict (structured from AE), JSON string, raw regex string,
list, and None. SQL injection is guarded by rejecting single quotes.
"""

import pytest

from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionTaskInput,
)


class TestFilterCoercion:
    """ExtractionInput accepts multiple filter formats."""

    def test_dict_filter_passes_through(self):
        """Native dict format from AE — pass through."""
        payload = {"include_filter": {"^qa$": ["^public$"]}}
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == {"^qa$": ["^public$"]}

    def test_string_filter_passes_through(self):
        """Raw regex string — pass through as string."""
        payload = {"exclude_filter": "^temp_.*$"}
        result = ExtractionInput.model_validate(payload)
        assert result.exclude_filter == "^temp_.*$"

    def test_json_string_stays_as_string(self):
        """JSON string — stays as string (parsed at usage time by sql_filters)."""
        payload = {"include_filter": '{"^qa$": ["^public$"]}'}
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == '{"^qa$": ["^public$"]}'

    def test_list_wrapped_as_dict(self):
        """List of regexes — wrapped as match-all-databases dict."""
        payload = {"include_filter": ["^db1$", "^db2$"]}
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == {".*": ["^db1$", "^db2$"]}

    def test_none_becomes_empty_string(self):
        """None → empty string."""
        payload = {"include_filter": None}
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == ""

    def test_empty_string_unchanged(self):
        """Empty string → stays empty string."""
        payload = {"include_filter": ""}
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == ""

    def test_default_is_empty_string(self):
        """No filter provided → default empty string."""
        result = ExtractionInput.model_validate({})
        assert result.include_filter == ""
        assert result.exclude_filter == ""


class TestFilterSQLInjectionGuard:
    """SQL-unsafe sequences blocked in filter values (BLDX-518 deny-list)."""

    def test_single_quote_in_string_rejected(self):
        payload = {"include_filter": "prefix'injection"}
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            ExtractionInput.model_validate(payload)

    def test_single_quote_in_dict_key_rejected(self):
        payload = {"include_filter": {"db'; DROP TABLE--": ["^public$"]}}
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            ExtractionInput.model_validate(payload)

    def test_single_quote_in_dict_value_rejected(self):
        payload = {"include_filter": {"^db$": ["schema'; DROP TABLE--"]}}
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            ExtractionInput.model_validate(payload)

    def test_semicolon_rejected(self):
        # Statement separator — would let an attacker stack a second
        # statement after the filter substitution.
        payload = {"include_filter": "name; DROP TABLE users"}
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence ';'"):
            ExtractionInput.model_validate(payload)

    def test_line_comment_rejected(self):
        # SQL line comment eats the rest of the line.
        payload = {"include_filter": "name-- comment"}
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence '--'"):
            ExtractionInput.model_validate(payload)

    def test_block_comment_open_rejected(self):
        payload = {"include_filter": "name/* injected */"}
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence '/\*'"):
            ExtractionInput.model_validate(payload)

    def test_null_byte_rejected(self):
        payload = {"include_filter": "name\x00trailing"}
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            ExtractionInput.model_validate(payload)

    def test_clean_string_passes(self):
        result = ExtractionInput.model_validate({"include_filter": "^prod_.*$"})
        assert result.include_filter == "^prod_.*$"

    def test_clean_dict_passes(self):
        result = ExtractionInput.model_validate(
            {"include_filter": {"^prod$": ["^analytics$"]}}
        )
        assert result.include_filter == {"^prod$": ["^analytics$"]}

    def test_regex_metachars_still_allowed(self):
        # The deny-list must not over-block legitimate regex syntax —
        # ``^``, ``$``, ``.``, ``*``, ``+``, ``?``, ``|``, ``()``, ``[]``,
        # ``\``, ``{}``, and single ``-`` all stay legal.
        payload = {"include_filter": r"^(prod|stage)_db\.[a-z]+(\.bak)?$"}
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == r"^(prod|stage)_db\.[a-z]+(\.bak)?$"


class TestTempTableRegexInjectionGuard:
    """temp_table_regex Pydantic validator blocks SQL injection (BLDX-518).

    Single-char forbidden sequences ('  " ; \\x00) are caught by
    Field(pattern=SAFE_FILTER_PATTERN) — Pydantic raises a
    string_pattern_mismatch error.  Multi-char sequences (-- /* */) are
    not matched by the regex and are caught by the @field_validator that
    calls validate_filter_no_sql_injection, which raises a ValueError
    with "SQL-unsafe sequence" in the message.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "evil--comment",
            "evil/* block",
            "evil */",
        ],
    )
    def test_multichar_sequence_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            ExtractionInput.model_validate({"temp_table_regex": value})

    @pytest.mark.parametrize(
        "value",
        [
            "evil; DROP TABLE x",
            "evil'injection",
        ],
    )
    def test_singlechar_sequence_rejected(self, value: str) -> None:
        with pytest.raises(ValueError):
            ExtractionInput.model_validate({"temp_table_regex": value})

    def test_clean_regex_passes(self) -> None:
        result = ExtractionInput.model_validate({"temp_table_regex": "^temp_.*$"})
        assert result.temp_table_regex == "^temp_.*$"

    def test_task_input_multichar_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            ExtractionTaskInput.model_validate({"temp_table_regex": "evil--"})


class TestExtractionTaskInputFilters:
    """ExtractionTaskInput has the same filter coercion."""

    def test_dict_filter_accepted(self):
        result = ExtractionTaskInput.model_validate(
            {"include_filter": {"^qa$": ["^public$"]}}
        )
        assert result.include_filter == {"^qa$": ["^public$"]}

    def test_string_filter_accepted(self):
        result = ExtractionTaskInput.model_validate({"exclude_filter": "^temp_.*$"})
        assert result.exclude_filter == "^temp_.*$"

    def test_none_becomes_empty_string(self):
        result = ExtractionTaskInput.model_validate({"include_filter": None})
        assert result.include_filter == ""


class TestRealAEPayload:
    """Simulate real AE payloads."""

    def test_ae_payload_with_pre_parsed_filters(self):
        """AE sends pre-parsed dicts — works natively."""
        payload = {
            "workflow_id": "test-wf-123",
            "credential_guid": "abc-def",
            "include_filter": {"^qa_test_db$": ["^public$"]},
            "exclude_filter": {},
            "extraction_method": "direct",
        }
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == {"^qa_test_db$": ["^public$"]}
        assert result.exclude_filter == {}

    def test_legacy_payload_with_string_filters(self):
        """Legacy connectors send strings — works as before."""
        payload = {
            "include_filter": '{"^prod$": ["^analytics$"]}',
            "exclude_filter": "^temp_.*$",
        }
        result = ExtractionInput.model_validate(payload)
        assert isinstance(result.include_filter, str)
        assert isinstance(result.exclude_filter, str)
