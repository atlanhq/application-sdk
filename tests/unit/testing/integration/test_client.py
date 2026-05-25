"""Unit tests for IntegrationTestClient credential conversion."""

from unittest.mock import patch

import pytest

from application_sdk.testing.integration.client import (
    IntegrationTestClient,
    _from_v3_credentials,
    _to_v3_credentials,
)


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


class TestFromV3Credentials:
    """Tests for the _from_v3_credentials helper."""

    def test_dict_pass_through(self):
        assert _from_v3_credentials({"host": "localhost"}) == {"host": "localhost"}

    def test_v3_list_to_flat_dict(self):
        creds = [
            {"key": "host", "value": "localhost"},
            {"key": "port", "value": "5432"},
        ]
        assert _from_v3_credentials(creds) == {"host": "localhost", "port": "5432"}

    def test_extra_keys_renested(self):
        creds = [
            {"key": "host", "value": "localhost"},
            {"key": "extra.ssl", "value": "true"},
            {"key": "extra.timeout", "value": "30"},
        ]
        result = _from_v3_credentials(creds)
        assert result["host"] == "localhost"
        assert result["extra"] == {"ssl": "true", "timeout": "30"}


class TestProvisionAndStartWorkflow:
    """Tests for the auto-provisioning behavior in _call_workflow.

    The vault refactor (commit 0a55371d) made /start strip inline credentials
    and require a credential_guid. These tests pin the framework's new contract:
    it must POST credentials to /dev/local-vault and forward only the guid.
    """

    def _client(self) -> IntegrationTestClient:
        return IntegrationTestClient(host="http://app", version="v1")

    def test_workflow_provisions_then_starts(self):
        client = self._client()
        responses = iter(
            [
                {"data": {"credential_guid": "guid-123"}},
                {"success": True, "data": {"workflow_id": "wf-1", "run_id": "run-1"}},
            ]
        )
        with patch.object(
            client, "_post", side_effect=lambda *_a, **_kw: next(responses)
        ) as post:
            response = client.call_api(
                "workflow",
                {
                    "credentials": {"host": "db", "username": "u"},
                    "metadata": {"foo": "bar"},
                },
            )

        assert response["success"] is True
        assert post.call_args_list[0].args[0] == "/dev/local-vault"
        # local-vault must receive flat dict, not v3 pairs
        vault_body = post.call_args_list[0].kwargs["data"]
        assert vault_body == {"host": "db", "username": "u"}

        start_body = post.call_args_list[1].kwargs["data"]
        assert start_body["credential_guid"] == "guid-123"
        assert "credentials" not in start_body
        assert start_body["metadata"] == {"foo": "bar"}

    def test_workflow_skips_provisioning_when_guid_provided(self):
        client = self._client()
        with patch.object(
            client,
            "_post",
            return_value={"success": True, "data": {"workflow_id": "w"}},
        ) as post:
            client.call_api(
                "workflow",
                {"credential_guid": "preset-guid", "metadata": {}},
            )

        assert post.call_count == 1
        endpoint = post.call_args.args[0]
        assert endpoint != "/dev/local-vault"

    def test_provision_raises_when_guid_missing(self):
        client = self._client()
        with (
            patch.object(
                client,
                "_post",
                return_value={"success": False, "error": "boom"},
            ),
            pytest.raises(RuntimeError, match="credential_guid"),
        ):
            client._provision_credentials({"host": "db"})

    def test_workflow_falls_back_to_inline_when_local_vault_gated_off(self):
        """SDR testcontainer / non-LOCAL deployments gate /dev/local-vault behind 403.

        The framework must not break those tests: provisioning returns None and
        the workflow start receives credentials inline (as v3 pairs).
        """
        client = self._client()
        responses = iter(
            [
                {"detail": "Dev-only endpoint", "_http_status": 403},
                {"success": True, "data": {"workflow_id": "w"}},
            ]
        )
        with patch.object(
            client, "_post", side_effect=lambda *_a, **_kw: next(responses)
        ) as post:
            client.call_api(
                "workflow",
                {"credentials": {"host": "db", "username": "u"}, "metadata": {}},
            )

        assert post.call_count == 2
        start_body = post.call_args_list[1].kwargs["data"]
        assert "credential_guid" not in start_body
        assert start_body["credentials"] == [
            {"key": "host", "value": "db"},
            {"key": "username", "value": "u"},
        ]

    def test_workflow_accepts_v3_credentials_list(self):
        """Scenarios that hand-craft v3 pair lists still get flattened for vault."""
        client = self._client()
        responses = iter(
            [
                {"data": {"credential_guid": "guid-xyz"}},
                {"success": True, "data": {"workflow_id": "w"}},
            ]
        )
        with patch.object(
            client, "_post", side_effect=lambda *_a, **_kw: next(responses)
        ) as post:
            client.call_api(
                "workflow",
                {
                    "credentials": [
                        {"key": "host", "value": "db"},
                        {"key": "extra.ssl", "value": "true"},
                    ]
                },
            )

        vault_body = post.call_args_list[0].kwargs["data"]
        assert vault_body == {"host": "db", "extra": {"ssl": "true"}}
