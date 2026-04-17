"""Unit tests for IntegrationTestClient credential conversion."""

from application_sdk.testing.integration.client import _to_v3_credentials


class TestToV3Credentials:
    """Tests for the _to_v3_credentials helper."""

    def test_flat_dict(self):
        result = _to_v3_credentials({"host": "localhost", "port": 5432})
        assert {"key": "host", "value": "localhost"} in result
        assert {"key": "port", "value": "5432"} in result

    def test_already_v3_format(self):
        v3_creds = [{"key": "host", "value": "localhost"}]
        assert _to_v3_credentials(v3_creds) is v3_creds

    def test_none_values_skipped(self):
        result = _to_v3_credentials({"host": "localhost", "port": None})
        assert len(result) == 1
        assert result[0]["key"] == "host"

    def test_extra_dict_flattened(self):
        result = _to_v3_credentials(
            {"host": "localhost", "extra": {"ssl": "true", "timeout": "30"}}
        )
        keys = {p["key"] for p in result}
        assert "host" in keys
        assert "extra.ssl" in keys
        assert "extra.timeout" in keys

    def test_bool_value_json_serialized(self):
        result = _to_v3_credentials({"ssl": True})
        assert result[0] == {"key": "ssl", "value": "true"}

    def test_dict_value_json_serialized(self):
        result = _to_v3_credentials({"config": {"nested": "value"}})
        assert result[0]["key"] == "config"
        assert (
            result[0]["value"] == '{"nested":"value"}'
            or '"nested"' in result[0]["value"]
        )

    def test_empty_dict(self):
        assert _to_v3_credentials({}) == []

    def test_does_not_mutate_input(self):
        original = {"host": "localhost", "extra": {"ssl": "true"}}
        _to_v3_credentials(original)
        # Original should still have "extra" key (we shallow-copy)
        assert "extra" in original
