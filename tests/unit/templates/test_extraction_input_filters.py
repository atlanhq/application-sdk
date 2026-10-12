"""Tests for ExtractionInput filter value handling (BLDX-1125).

Filters accept dict (structured from AE), JSON string, raw regex string,
list, and None. SQL injection is guarded by rejecting single quotes.
"""

from typing import Annotated

import pytest
from pydantic import Field, ValidationError

from application_sdk.contracts.types import MaxItems
from application_sdk.templates.contracts.incremental_sql import (
    IncrementalExtractionInput,
)
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

    def test_deeper_apitree_filter_truncated_to_catalog_schema_level(self):
        payload = {
            "include_filter": {
                "AwsDataCatalog": {
                    "mswtest_2": {
                        "nested_schema": {},
                    },
                }
            }
        }
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == {"AwsDataCatalog": ["mswtest_2"]}

    def test_overridden_filter_fields_keep_app_specific_tree_shape(self):
        class AppSpecificInput(ExtractionInput, allow_unbounded_fields=True):
            include_filter: Annotated[dict[str, object], MaxItems(1000)] = Field(  # type: ignore[assignment]
                default_factory=dict
            )
            exclude_filter: Annotated[dict[str, object], MaxItems(1000)] = Field(  # type: ignore[assignment]
                default_factory=dict
            )

        payload = {
            "include_filter": {
                "warehouse_1": {
                    "project_1": {
                        "dataset_1": {},
                    },
                }
            },
            "exclude_filter": {
                "warehouse_2": {},
            },
        }
        result = AppSpecificInput.model_validate(payload)
        assert result.include_filter == {
            "warehouse_1": {
                "project_1": {
                    "dataset_1": {},
                },
            }
        }
        assert result.exclude_filter == {"warehouse_2": {}}


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


# The generated connector contract, verbatim (fields only) from
# atlanhq/atlan-mysql-app ``app/generated/_input.py`` (origin/main): the
# contract toolkit redeclares ``exclude_table_regex`` as a plain ``str`` on every
# SQL app's ``AppInputContract(ExtractionInput)``.
class _GeneratedAppInputContract(ExtractionInput):
    exclude_table_regex: str = ""
    """Regular expression to exclude temporary tables and views."""
    preflight_check: str = ""
    output_dir: str = ""
    checkpoint_dir: str = ""
    load_to_atlan: bool = True
    publish_dry_run: bool = False


# The mysql form's placeholder for "Exclude regex for tables & views".
_MYSQL_FORM_PLACEHOLDER = ".*_TMP|.*_TEMP|TMP:*|TEMP:*"

_INPUT_TYPES = [ExtractionInput, _GeneratedAppInputContract]


class TestExcludeTableRegexRouting:
    """The form's ``exclude_table_regex`` reaches ``temp_table_regex`` (FND-2733).

    The toolkit emits the form key ``exclude-table-regex`` as the workflow arg
    ``exclude_table_regex``; the SDK only ever renders ``temp_table_regex``
    into the table/column SQL. Before the fix the key was dropped as unknown
    on ``ExtractionInput`` and ignored on the generated subclass.
    """

    @pytest.mark.parametrize("input_type", _INPUT_TYPES)
    def test_flat_arg_is_routed(self, input_type: type[ExtractionInput]) -> None:
        result = input_type.model_validate({"exclude_table_regex": "^tmp_"})
        assert result.temp_table_regex == "^tmp_"

    @pytest.mark.parametrize("input_type", _INPUT_TYPES)
    @pytest.mark.parametrize("key", ["exclude-table-regex", "exclude_table_regex"])
    def test_nested_metadata_is_routed(
        self, input_type: type[ExtractionInput], key: str
    ) -> None:
        result = input_type.model_validate({"metadata": {key: "^tmp_"}})
        assert result.temp_table_regex == "^tmp_"

    def test_top_level_kebab_key_is_routed(self) -> None:
        result = ExtractionInput.model_validate({"exclude-table-regex": "^tmp_"})
        assert result.temp_table_regex == "^tmp_"

    @pytest.mark.parametrize("input_type", _INPUT_TYPES)
    def test_explicit_temp_table_regex_wins(
        self, input_type: type[ExtractionInput]
    ) -> None:
        result = input_type.model_validate(
            {"temp_table_regex": "^explicit$", "exclude_table_regex": "^form$"}
        )
        assert result.temp_table_regex == "^explicit$"

    def test_explicit_nested_temp_table_regex_wins(self) -> None:
        result = ExtractionInput.model_validate(
            {
                "metadata": {
                    "temp-table-regex": "^explicit$",
                    "exclude-table-regex": "^form$",
                }
            }
        )
        assert result.temp_table_regex == "^explicit$"

    @pytest.mark.parametrize("input_type", _INPUT_TYPES)
    @pytest.mark.parametrize("value", ["", None])
    def test_empty_or_null_leaves_filter_off(
        self, input_type: type[ExtractionInput], value: str | None
    ) -> None:
        result = input_type.model_validate({"exclude_table_regex": value})
        assert result.temp_table_regex == ""
        assert result.exclude_table_regex == ""

    @pytest.mark.parametrize("input_type", _INPUT_TYPES)
    def test_mysql_form_placeholder_accepted(
        self, input_type: type[ExtractionInput]
    ) -> None:
        result = input_type.model_validate(
            {"exclude_table_regex": _MYSQL_FORM_PLACEHOLDER}
        )
        assert result.temp_table_regex == _MYSQL_FORM_PLACEHOLDER

    @pytest.mark.parametrize("input_type", _INPUT_TYPES)
    @pytest.mark.parametrize(
        "payload",
        [
            {"exclude_table_regex": "x' OR '1'='1"},
            {"metadata": {"exclude-table-regex": "x' OR '1'='1"}},
            {"exclude_table_regex": "tmp_--"},
            {"exclude_table_regex": "tmp_*/ OR 1=1 /*"},
        ],
    )
    def test_injection_payload_rejected(
        self, input_type: type[ExtractionInput], payload: dict
    ) -> None:
        with pytest.raises(ValidationError):
            input_type.model_validate(payload)

    def test_injection_in_ignored_form_value_still_rejected(self) -> None:
        # An explicit temp_table_regex wins, but the form value is still a
        # declared field with the same deny-list, so a subclass that reads it
        # directly never sees an unsafe string.
        with pytest.raises(ValidationError):
            _GeneratedAppInputContract.model_validate(
                {"temp_table_regex": "^ok$", "exclude_table_regex": "x' OR '1'='1"}
            )

    def test_legacy_quoted_csv_is_normalised(self) -> None:
        result = ExtractionInput.model_validate(
            {"exclude_table_regex": '"TMP_A","TMP_B"'}
        )
        assert result.temp_table_regex == "TMP_A|TMP_B"

    def test_incremental_input_inherits_routing(self) -> None:
        result = IncrementalExtractionInput.model_validate(
            {"exclude_table_regex": "^tmp_"}
        )
        assert result.temp_table_regex == "^tmp_"

    def test_declared_so_not_an_unknown_key(self) -> None:
        # Declared on the SDK contract, so the unknown-key warning no longer
        # names it and schema consumers see it as SDK-provided.
        assert "exclude_table_regex" in ExtractionInput.model_fields
