"""Tests for ExtractionInput filter value coercion (BLDX-1118).

AE pre-parses JSON filter strings into dicts before passing to Temporal.
ExtractionInput must coerce them back to strings.
"""

import json

from application_sdk.templates.contracts.sql_metadata import ExtractionInput


class TestFilterValueCoercion:
    def test_dict_include_filter_coerced_to_json_string(self):
        """AE sends include_filter as dict — should be JSON-serialized."""
        payload = {
            "include_filter": {"^qa_test_db$": ["^public$"]},
        }
        result = ExtractionInput.model_validate(payload)
        assert isinstance(result.include_filter, str)
        assert json.loads(result.include_filter) == {"^qa_test_db$": ["^public$"]}

    def test_dict_exclude_filter_coerced_to_json_string(self):
        """AE sends exclude_filter as dict — should be JSON-serialized."""
        payload = {
            "exclude_filter": {"^temp_.*$": [".*"]},
        }
        result = ExtractionInput.model_validate(payload)
        assert isinstance(result.exclude_filter, str)
        assert json.loads(result.exclude_filter) == {"^temp_.*$": [".*"]}

    def test_list_filter_coerced_to_json_string(self):
        """Filter value as list should also be JSON-serialized."""
        payload = {
            "include_filter": ["^db1$", "^db2$"],
        }
        result = ExtractionInput.model_validate(payload)
        assert isinstance(result.include_filter, str)
        assert json.loads(result.include_filter) == ["^db1$", "^db2$"]

    def test_string_filter_unchanged(self):
        """Normal string filter should pass through unchanged."""
        payload = {
            "include_filter": '{"^qa$": ["^public$"]}',
            "exclude_filter": "^temp_.*$",
        }
        result = ExtractionInput.model_validate(payload)
        assert result.include_filter == '{"^qa$": ["^public$"]}'
        assert result.exclude_filter == "^temp_.*$"

    def test_empty_string_filter_unchanged(self):
        """Empty string defaults should work."""
        result = ExtractionInput.model_validate({})
        assert result.include_filter == ""
        assert result.exclude_filter == ""
        assert result.temp_table_regex == ""

    def test_dict_temp_table_regex_coerced(self):
        """temp_table_regex as dict should be JSON-serialized."""
        payload = {
            "temp_table_regex": {"pattern": "^tmp_.*"},
        }
        result = ExtractionInput.model_validate(payload)
        assert isinstance(result.temp_table_regex, str)

    def test_multiple_filters_all_coerced(self):
        """All three filter fields coerced in single payload."""
        payload = {
            "include_filter": {"^prod$": ["^analytics$"]},
            "exclude_filter": {"^test_.*$": [".*"]},
            "temp_table_regex": ["^tmp_", "^temp_"],
        }
        result = ExtractionInput.model_validate(payload)
        assert isinstance(result.include_filter, str)
        assert isinstance(result.exclude_filter, str)
        assert isinstance(result.temp_table_regex, str)

    def test_none_filter_stays_default(self):
        """None filter values should fall through to default empty string."""
        payload = {
            "include_filter": None,
        }
        result = ExtractionInput.model_validate(payload)
        # None is not dict/list, so validator doesn't touch it — Pydantic uses default
        assert result.include_filter == ""

    def test_real_ae_payload(self):
        """Simulate a real AE payload with pre-parsed filters."""
        payload = {
            "workflow_id": "test-wf-123",
            "credential_guid": "abc-def",
            "include_filter": {"^qa_test_db$": ["^public$"]},
            "exclude_filter": {},
            "extraction_method": "direct",
        }
        result = ExtractionInput.model_validate(payload)
        assert result.workflow_id == "test-wf-123"
        assert result.credential_guid == "abc-def"
        assert isinstance(result.include_filter, str)
        assert result.include_filter == '{"^qa_test_db$": ["^public$"]}'
        assert result.exclude_filter == "{}"
