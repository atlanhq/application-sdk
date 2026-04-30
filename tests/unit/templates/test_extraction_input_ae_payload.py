"""Tests for ExtractionInput AE payload normalization (_normalize_ae_payload).

AE nests config fields inside a metadata{} dict and may use hyphenated keys.
The validator lifts them to the top level with underscored names.
"""

from application_sdk.templates.contracts.sql_metadata import ExtractionInput


class TestMetadataLifting:
    def test_fields_inside_metadata_lifted_to_top_level(self):
        payload = {
            "workflow_id": "test-123",
            "metadata": {
                "extraction_method": "agent",
                "credential_guid": "abc-123",
            },
        }
        result = ExtractionInput.model_validate(payload)
        assert result.extraction_method == "agent"
        assert result.credential_guid == "abc-123"

    def test_agent_json_lifted_from_metadata(self):
        payload = {
            "metadata": {
                "extraction_method": "agent",
                "agent_json": {
                    "agent-name": "test-agent",
                    "secret-manager": "awssecretmanager",
                    "secret-path": "atlan/dev/test",
                    "auth-type": "basic",
                },
            },
        }
        result = ExtractionInput.model_validate(payload)
        assert result.extraction_method == "agent"
        assert result.agent_json is not None
        assert result.agent_json.agent_name == "test-agent"

    def test_top_level_values_not_overwritten_by_metadata(self):
        payload = {
            "credential_guid": "top-level-guid",
            "metadata": {
                "credential_guid": "metadata-guid",
            },
        }
        result = ExtractionInput.model_validate(payload)
        assert result.credential_guid == "top-level-guid"

    def test_non_dict_input_passes_through(self):
        result = ExtractionInput.model_validate(
            {"workflow_id": "test", "credential_guid": "abc"}
        )
        assert result.workflow_id == "test"
        assert result.credential_guid == "abc"

    def test_empty_metadata_is_harmless(self):
        payload = {"credential_guid": "abc", "metadata": {}}
        result = ExtractionInput.model_validate(payload)
        assert result.credential_guid == "abc"

    def test_metadata_non_dict_is_ignored(self):
        payload = {"credential_guid": "abc", "metadata": "not-a-dict"}
        result = ExtractionInput.model_validate(payload)
        assert result.credential_guid == "abc"


class TestHyphenatedKeyNormalization:
    def test_hyphenated_top_level_keys_normalized(self):
        payload = {
            "credential-guid": "abc-123",
        }
        result = ExtractionInput.model_validate(payload)
        assert result.credential_guid == "abc-123"

    def test_hyphenated_keys_inside_metadata_normalized(self):
        payload = {
            "metadata": {
                "credential-guid": "abc-123",
                "extraction-method": "direct",
            },
        }
        result = ExtractionInput.model_validate(payload)
        assert result.credential_guid == "abc-123"
        assert result.extraction_method == "direct"

    def test_underscore_key_takes_precedence_over_hyphenated(self):
        payload = {
            "credential_guid": "underscore-wins",
            "credential-guid": "hyphen-loses",
        }
        result = ExtractionInput.model_validate(payload)
        assert result.credential_guid == "underscore-wins"
