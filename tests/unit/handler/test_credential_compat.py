"""Tests for v2 → v3 credential format normalization.

V2 callers (platform UI, internal services) send flat credential dicts.
V3 expects structured HandlerCredential lists. The model_validator on
AuthInput, PreflightInput, and MetadataInput normalizes v2 format automatically.
"""

import json

from application_sdk.handler.contracts import (
    AuthInput,
    MetadataInput,
    PreflightInput,
    _normalize_v2_credentials,
)


class TestV3Passthrough:
    """V3-native payloads pass through unchanged."""

    def test_v3_credentials_list_unchanged(self):
        data = {"credentials": [{"key": "host", "value": "myhost"}]}
        result = _normalize_v2_credentials(data)
        assert result is data  # Same object — no copy

    def test_v3_empty_credentials_list(self):
        data = {"credentials": []}
        result = _normalize_v2_credentials(data)
        assert result is data


class TestV2FlatConversion:
    """V2 flat dicts are converted to v3 credential lists."""

    def test_basic_flat_credentials(self):
        data = {
            "authType": "basic",
            "host": "myhost",
            "port": 5432,
            "username": "admin",
            "password": "secret",
        }
        result = _normalize_v2_credentials(data)
        creds = {c["key"]: c["value"] for c in result["credentials"]}
        assert creds["host"] == "myhost"
        assert creds["port"] == "5432"
        assert creds["username"] == "admin"
        assert creds["password"] == "secret"
        assert creds["authType"] == "basic"
        # Original keys removed from top level
        assert "host" not in result
        assert "port" not in result

    def test_extra_dict_flattened(self):
        data = {"host": "x", "extra": {"ssl": "true", "warehouse": "wh1"}}
        result = _normalize_v2_credentials(data)
        creds = {c["key"]: c["value"] for c in result["credentials"]}
        assert creds["host"] == "x"
        assert creds["ssl"] == "true"
        assert creds["warehouse"] == "wh1"
        assert "extra" not in creds  # extra itself is not a credential

    def test_nested_dict_json_serialized(self):
        data = {"host": "x", "config": {"a": 1, "b": 2}}
        result = _normalize_v2_credentials(data)
        creds = {c["key"]: c["value"] for c in result["credentials"]}
        assert json.loads(creds["config"]) == {"a": 1, "b": 2}

    def test_none_value_becomes_empty_string(self):
        data = {"host": "x", "port": None}
        result = _normalize_v2_credentials(data)
        creds = {c["key"]: c["value"] for c in result["credentials"]}
        assert creds["port"] == ""

    def test_bool_value_stringified(self):
        data = {"host": "x", "ssl": True}
        result = _normalize_v2_credentials(data)
        creds = {c["key"]: c["value"] for c in result["credentials"]}
        assert creds["ssl"] == "True"

    def test_empty_body_passes_through(self):
        data: dict = {}
        result = _normalize_v2_credentials(data)
        assert result == {}

    def test_v3_known_fields_preserved(self):
        data = {"host": "x", "connection_id": "abc-123", "timeout_seconds": 30}
        result = _normalize_v2_credentials(data)
        creds = {c["key"]: c["value"] for c in result["credentials"]}
        assert "host" in creds
        assert "connection_id" not in creds  # v3 field, not a credential
        assert result["connection_id"] == "abc-123"
        assert result["timeout_seconds"] == 30

    def test_extra_keys_win_over_top_level_duplicates(self):
        data = {"ssl": "false", "extra": {"ssl": "true"}}
        result = _normalize_v2_credentials(data)
        creds = {c["key"]: c["value"] for c in result["credentials"]}
        # extra is processed after top-level in the dict, so extra wins
        assert creds["ssl"] == "true"


class TestModelValidators:
    """All three input models normalize v2 credentials via model_validator."""

    V2_PAYLOAD = {
        "authType": "basic",
        "host": "myhost.example.com",
        "port": 5432,
        "username": "admin",
        "password": "secret",
        "extra": {"ssl": "true", "warehouse": "wh1"},
    }

    def test_auth_input_normalizes(self):
        model = AuthInput.model_validate(self.V2_PAYLOAD)
        creds = {c.key: c.value for c in model.credentials}
        assert creds["host"] == "myhost.example.com"
        assert creds["username"] == "admin"
        assert creds["ssl"] == "true"

    def test_preflight_input_normalizes(self):
        model = PreflightInput.model_validate(self.V2_PAYLOAD)
        creds = {c.key: c.value for c in model.credentials}
        assert creds["host"] == "myhost.example.com"
        assert creds["password"] == "secret"

    def test_metadata_input_normalizes(self):
        model = MetadataInput.model_validate(self.V2_PAYLOAD)
        creds = {c.key: c.value for c in model.credentials}
        assert creds["host"] == "myhost.example.com"
        assert creds["warehouse"] == "wh1"

    def test_v3_payload_passes_through_all_models(self):
        v3 = {"credentials": [{"key": "host", "value": "x"}]}
        for cls in (AuthInput, PreflightInput, MetadataInput):
            model = cls.model_validate(v3)
            assert len(model.credentials) == 1
            assert model.credentials[0].key == "host"
