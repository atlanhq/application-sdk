"""Tests for ExtractionInput filter value handling (BLDX-1125).

Filters accept dict (structured from AE), JSON string, raw regex string,
list, and None. SQL injection is guarded by rejecting single quotes.
"""

import logging

import pytest

from application_sdk.contracts.base import Input
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
    """Single quotes blocked in filter values."""

    def test_single_quote_in_string_rejected(self):
        payload = {"include_filter": "prefix'injection"}
        with pytest.raises(ValueError, match="Single quotes not allowed"):
            ExtractionInput.model_validate(payload)

    def test_single_quote_in_dict_key_rejected(self):
        payload = {"include_filter": {"db'; DROP TABLE--": ["^public$"]}}
        with pytest.raises(ValueError, match="Single quotes not allowed"):
            ExtractionInput.model_validate(payload)

    def test_single_quote_in_dict_value_rejected(self):
        payload = {"include_filter": {"^db$": ["schema'; DROP TABLE--"]}}
        with pytest.raises(ValueError, match="Single quotes not allowed"):
            ExtractionInput.model_validate(payload)

    def test_clean_string_passes(self):
        result = ExtractionInput.model_validate({"include_filter": "^prod_.*$"})
        assert result.include_filter == "^prod_.*$"

    def test_clean_dict_passes(self):
        result = ExtractionInput.model_validate(
            {"include_filter": {"^prod$": ["^analytics$"]}}
        )
        assert result.include_filter == {"^prod$": ["^analytics$"]}


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


class TestAENestedMetadataLift:
    """AE wraps extraction config under ``metadata``; SDK must lift it."""

    def test_dict_filters_nested_in_metadata_are_lifted(self):
        payload = {
            "connection": {
                "typeName": "Connection",
                "attributes": {"qualifiedName": "default/x/1", "name": "n"},
            },
            "credential_guid": "g",
            "metadata": {
                "include_filter": {"^prod$": ["^analytics$"]},
                "exclude_filter": {"^temp$": [".*"]},
                "extraction_method": "direct",
            },
        }

        result = ExtractionInput.model_validate(payload)

        assert result.include_filter == {"^prod$": ["^analytics$"]}
        assert result.exclude_filter == {"^temp$": [".*"]}
        assert result.extraction_method == "direct"

    def test_top_level_value_wins_over_nested(self):
        payload = {
            "connection": {
                "typeName": "Connection",
                "attributes": {"qualifiedName": "default/x/1", "name": "n"},
            },
            "credential_guid": "g",
            "extraction_method": "agent",
            "metadata": {"extraction_method": "direct"},
        }

        assert ExtractionInput.model_validate(payload).extraction_method == "agent"

    def test_hyphenated_keys_in_metadata_are_normalised(self):
        payload = {
            "connection": {
                "typeName": "Connection",
                "attributes": {"qualifiedName": "default/x/1", "name": "n"},
            },
            "credential_guid": "g",
            "metadata": {
                "include-filter": {"^prod$": ["^a$"]},
                "extraction-method": "direct",
            },
        }

        result = ExtractionInput.model_validate(payload)

        assert result.include_filter == {"^prod$": ["^a$"]}
        assert result.extraction_method == "direct"

    def test_top_level_hyphenated_keys_are_normalised(self):
        payload = {
            "credential-guid": "g",
            "include-filter": {"^prod$": ["^a$"]},
            "extraction-method": "direct",
        }

        result = ExtractionInput.model_validate(payload)

        assert result.credential_guid == "g"
        assert result.include_filter == {"^prod$": ["^a$"]}
        assert result.extraction_method == "direct"

    def test_string_filters_in_metadata_still_work(self):
        payload = {
            "connection": {
                "typeName": "Connection",
                "attributes": {"qualifiedName": "default/x/1", "name": "n"},
            },
            "credential_guid": "g",
            "metadata": {"include_filter": "^prod_.*$"},
        }

        assert ExtractionInput.model_validate(payload).include_filter == "^prod_.*$"

    def test_empty_metadata_does_not_overwrite_defaults(self):
        payload = {
            "connection": {
                "typeName": "Connection",
                "attributes": {"qualifiedName": "default/x/1", "name": "n"},
            },
            "credential_guid": "g",
            "metadata": {},
        }

        result = ExtractionInput.model_validate(payload)

        assert result.include_filter == ""
        assert result.exclude_filter == ""

    def test_nested_direct_agent_json_is_skipped_after_lift(self):
        payload = {
            "credential_guid": "g",
            "metadata": {
                "extraction_method": "direct",
                "agent_json": '{"host": "host", "port": "port"}',
            },
        }

        result = ExtractionInput.model_validate(payload)

        assert result.extraction_method == "direct"
        assert result.agent_json is None

    def test_known_metadata_wrapper_does_not_warn(self, caplog):
        Input._unknown_keys_seen.clear()
        caplog.set_level(logging.WARNING, logger="application_sdk.contracts.base")

        ExtractionInput.model_validate(
            {
                "metadata": {
                    "include_filter": {"^prod$": ["^analytics$"]},
                    "exclude_filter": {"^temp$": [".*"]},
                    "extraction_method": "direct",
                }
            }
        )

        assert "Unknown keys in payload for ExtractionInput" not in caplog.text

    def test_unknown_metadata_key_still_warns(self, caplog):
        Input._unknown_keys_seen.clear()
        caplog.set_level(logging.WARNING, logger="application_sdk.contracts.base")

        result = ExtractionInput.model_validate(
            {
                "metadata": {
                    "include_filter": {"^prod$": ["^analytics$"]},
                    "future_field": "new-ae-config",
                }
            }
        )

        assert result.include_filter == {"^prod$": ["^analytics$"]}
        assert "Unknown keys in payload for ExtractionInput" in caplog.text
        assert "['metadata']" in caplog.text
