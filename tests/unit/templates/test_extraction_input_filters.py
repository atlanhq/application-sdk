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


class TestAPITreeFilterCoercion:
    """APITree include/exclude filters decode before app workflow code starts."""

    def test_apitree_exclude_filter_normalized_to_filter_map(self):
        payload = {
            "exclude_filter": {
                "AwsDataCatalog": {
                    "mswtest_2": {},
                    "mswtest_3": {},
                    "redshift_sample_testing": {},
                }
            }
        }
        result = ExtractionInput.model_validate(payload)
        assert result.exclude_filter == {
            "AwsDataCatalog": [
                "mswtest_2",
                "mswtest_3",
                "redshift_sample_testing",
            ]
        }

    def test_apitree_include_filter_lifted_from_ae_metadata(self):
        payload = {
            "metadata": {
                "include-filter": {
                    "AwsDataCatalog": {
                        "mswtest_2": {},
                    }
                }
            }
        }
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == {"AwsDataCatalog": ["mswtest_2"]}

    def test_apitree_filter_works_for_generated_contract_subclasses(self):
        class AppInputContract(ExtractionInput):
            pass

        payload = {
            "include_filter": {
                "AwsDataCatalog": {
                    "mswtest_2": {},
                }
            }
        }
        result = AppInputContract.model_validate(payload)
        assert result.include_filter == {"AwsDataCatalog": ["mswtest_2"]}

    def test_apitree_filter_works_for_task_inputs(self):
        payload = {
            "exclude_filter": {
                "AwsDataCatalog": {
                    "mswtest_2": {},
                }
            }
        }
        result = ExtractionTaskInput.model_validate(payload)
        assert result.exclude_filter == {"AwsDataCatalog": ["mswtest_2"]}


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

    def test_single_quote_in_nested_apitree_value_rejected(self):
        payload = {"include_filter": {"AwsDataCatalog": {"db'; DROP TABLE--": {}}}}
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            ExtractionInput.model_validate(payload)

    def test_single_quote_in_deeper_apitree_descendant_rejected(self):
        payload = {
            "include_filter": {
                "AwsDataCatalog": {
                    "mswtest_2": {
                        "schema'; DROP TABLE--": {},
                    }
                }
            }
        }
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


class TestLegacyQuotedCsvNormalisation:
    """Pre-v3 SaaS-agent quoted-CSV shape is auto-translated (HYP-1560).

    Migrated workflow specs carry filter values like ``'"A","B"'`` that
    the old agent parsed as a list of prefixes. The v3 contract takes
    a single regex string and the BLDX-518 deny-list rejects the
    quote/comma characters that shape requires — so without this
    translation, every migrated tenant on the v3.13 SDK fails at
    workflow-input decoding.
    """

    def test_temp_table_regex_legacy_csv_is_translated(self):
        # Canonical migrated value: two prefixes the old agent OR'd
        # together as separate LIKE patterns. Translate to a v3
        # alternation regex so the connector's regex engine actually
        # matches against table names.
        payload = {"temp_table_regex": '"PREFIX_A.","PREFIX_B."'}
        result = ExtractionInput.model_validate(payload)
        assert result.temp_table_regex == "PREFIX_A.|PREFIX_B."

    def test_temp_table_regex_v3_value_passes_through(self):
        # A value that's already a valid v3 regex must not be mangled
        # by the normaliser.
        payload = {"temp_table_regex": "PREFIX_A.|PREFIX_B."}
        result = ExtractionInput.model_validate(payload)
        assert result.temp_table_regex == "PREFIX_A.|PREFIX_B."

    def test_temp_table_regex_legacy_with_forbidden_item_still_rejected(self):
        # Defence in depth: if a legacy item smuggles a forbidden
        # sequence (e.g. ``--``), the normaliser's post-unwrap check
        # raises — the bypass route cannot launder injection through
        # the legacy shape.
        payload = {"temp_table_regex": '"prefix","name--bad"'}
        with pytest.raises(ValueError, match=r"SQL-unsafe sequence"):
            ExtractionInput.model_validate(payload)

    def test_include_filter_legacy_csv_is_translated(self):
        # Same translation for the FilterMap | str fields when the
        # string side of the union carries a legacy value.
        payload = {"include_filter": '"prod","stage"'}
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == "prod|stage"

    def test_exclude_filter_legacy_csv_is_translated(self):
        payload = {"exclude_filter": '"temp_a","temp_b"'}
        result = ExtractionInput.model_validate(payload)
        assert result.exclude_filter == "temp_a|temp_b"

    def test_json_string_filter_is_not_mangled(self):
        # JSON-shaped strings start with ``{`` and never match the
        # legacy detector, so they pass through unchanged for the
        # downstream parser.
        payload = {"include_filter": '{"^prod$": ["^analytics$"]}'}
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == '{"^prod$": ["^analytics$"]}'

    def test_extraction_task_input_temp_table_regex_translated(self):
        # The translation has to fire on ExtractionTaskInput too —
        # per-task inputs are reconstructed from the workflow spec
        # and re-validate the same fields.
        payload = {"temp_table_regex": '"PREFIX_A.","PREFIX_B."'}
        result = ExtractionTaskInput.model_validate(payload)
        assert result.temp_table_regex == "PREFIX_A.|PREFIX_B."

    def test_extraction_task_input_filter_translated(self):
        payload = {"exclude_filter": '"temp_a","temp_b"'}
        result = ExtractionTaskInput.model_validate(payload)
        assert result.exclude_filter == "temp_a|temp_b"
        assert isinstance(result.exclude_filter, str)
