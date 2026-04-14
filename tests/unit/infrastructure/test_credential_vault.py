"""Unit tests for CredentialVault and DaprCredentialVault."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.infrastructure._dapr.client import (
    DaprCredentialVault,
    _handle_single_key_secret,
    _process_secret_data,
    _resolve_credentials,
)
from application_sdk.infrastructure.credential_vault import (
    CredentialVault,
    CredentialVaultError,
    InMemoryCredentialVault,
)

# ---------------------------------------------------------------------------
# InMemoryCredentialVault
# ---------------------------------------------------------------------------


class TestInMemoryCredentialVault:
    async def test_get_credentials_returns_stored_dict(self) -> None:
        vault = InMemoryCredentialVault(
            {"guid-1": {"host": "db.example.com", "port": 5432}}
        )
        result = await vault.get_credentials("guid-1")
        assert result == {"host": "db.example.com", "port": 5432}

    async def test_get_credentials_raises_for_missing_guid(self) -> None:
        vault = InMemoryCredentialVault()
        with pytest.raises(CredentialVaultError) as exc_info:
            await vault.get_credentials("nonexistent")
        assert "nonexistent" in str(exc_info.value)

    async def test_set_then_get(self) -> None:
        vault = InMemoryCredentialVault()
        vault.set("abc", {"user": "admin"})
        result = await vault.get_credentials("abc")
        assert result["user"] == "admin"

    async def test_get_returns_copy(self) -> None:
        """Mutations to the returned dict should not affect the stored value."""
        vault = InMemoryCredentialVault({"g": {"key": "value"}})
        result = await vault.get_credentials("g")
        result["key"] = "mutated"
        result2 = await vault.get_credentials("g")
        assert result2["key"] == "value"

    async def test_clear_removes_all(self) -> None:
        vault = InMemoryCredentialVault({"g": {"k": "v"}})
        vault.clear()
        with pytest.raises(CredentialVaultError):
            await vault.get_credentials("g")

    def test_satisfies_protocol(self) -> None:
        vault = InMemoryCredentialVault()
        assert isinstance(vault, CredentialVault)


# ---------------------------------------------------------------------------
# _handle_single_key_secret
# ---------------------------------------------------------------------------


class TestHandleSingleKeySecret:
    def test_json_dict_value_is_unwrapped(self) -> None:
        nested = {"username": "u", "password": "p"}
        result = _handle_single_key_secret("key", json.dumps(nested))
        assert result == nested

    def test_json_empty_dict_is_returned(self) -> None:
        result = _handle_single_key_secret("key", "{}")
        assert result == {}

    def test_json_array_returns_key_value_pair(self) -> None:
        result = _handle_single_key_secret("key", json.dumps([1, 2]))
        assert result == {"key": json.dumps([1, 2])}

    def test_plain_string_returns_key_value_pair(self) -> None:
        result = _handle_single_key_secret("key", "plain")
        assert result == {"key": "plain"}

    def test_invalid_json_returns_key_value_pair(self) -> None:
        result = _handle_single_key_secret("key", "not json {")
        assert result == {"key": "not json {"}

    def test_non_string_value_returns_key_value_pair(self) -> None:
        assert _handle_single_key_secret("k", 42) == {"k": 42}
        assert _handle_single_key_secret("k", True) is not None
        assert _handle_single_key_secret("k", [1, 2]) == {"k": [1, 2]}


# ---------------------------------------------------------------------------
# _process_secret_data
# ---------------------------------------------------------------------------


class TestProcessSecretData:
    def test_multi_key_dict_returned_as_is(self) -> None:
        data = {"a": "1", "b": "2"}
        assert _process_secret_data(data) == data

    def test_single_key_with_json_dict_value_is_unwrapped(self) -> None:
        nested = {"username": "u", "password": "p"}
        result = _process_secret_data({"data": json.dumps(nested)})
        assert result == nested

    def test_single_key_plain_value_returned_as_is(self) -> None:
        result = _process_secret_data({"key": "value"})
        assert result == {"key": "value"}

    def test_scalar_map_container_coerced_to_dict(self) -> None:
        """Verify collections.abc.Mapping inputs are accepted."""
        import collections

        class FakeMapping(collections.abc.Mapping):
            def __init__(self, d: dict) -> None:
                self._d = d

            def __getitem__(self, k: str) -> Any:
                return self._d[k]

            def __iter__(self):
                return iter(self._d)

            def __len__(self) -> int:
                return len(self._d)

        result = _process_secret_data(FakeMapping({"k": "v"}))
        assert result == {"k": "v"}


# ---------------------------------------------------------------------------
# _resolve_credentials
# ---------------------------------------------------------------------------


class TestResolveCredentials:
    def test_simple_substitution(self) -> None:
        config = {"host": "db.example.com", "password": "pass_key"}
        secrets = {"pass_key": "actual_pass"}
        result = _resolve_credentials(config, secrets)
        assert result["password"] == "actual_pass"
        assert result["host"] == "db.example.com"

    def test_extra_field_substitution(self) -> None:
        config = {"host": "h", "extra": {"ssl_cert": "cert_key"}}
        secrets = {"cert_key": "-----BEGIN CERT-----"}
        result = _resolve_credentials(config, secrets)
        assert result["extra"]["ssl_cert"] == "-----BEGIN CERT-----"

    def test_no_matching_key_leaves_original(self) -> None:
        config = {"password": "literal_password"}
        result = _resolve_credentials(config, {"unrelated": "val"})
        assert result["password"] == "literal_password"

    def test_returns_deep_copy(self) -> None:
        config = {"k": "ref"}
        secrets = {"ref": "resolved"}
        result = _resolve_credentials(config, secrets)
        result["k"] = "mutated"
        # Original config must be unmodified.
        assert config["k"] == "ref"


# ---------------------------------------------------------------------------
# DaprCredentialVault.get_credentials (mocked Dapr)
# ---------------------------------------------------------------------------


def _make_mock_dapr_client(
    config_bytes: bytes | None,
    secret_data: dict[str, str],
) -> MagicMock:
    """Build a mock DaprClient that returns *config_bytes* on invoke_binding
    and *secret_data* on get_secret."""
    mock_client = MagicMock()

    # Binding response
    binding_response = MagicMock()
    binding_response.data = config_bytes
    binding_response.binding_metadata = {}
    mock_client.invoke_binding.return_value = binding_response

    # Secret response
    secret_response = MagicMock()
    secret_response.secret = secret_data
    mock_client.get_secret.return_value = secret_response

    return mock_client


class TestDaprCredentialVaultGetCredentials:
    """Tests for DaprCredentialVault.get_credentials with mocked Dapr."""

    def _make_vault(self, mock_client: MagicMock) -> DaprCredentialVault:
        return DaprCredentialVault(
            mock_client,
            upstream_binding_name="upstream-objectstore",
            secret_store_name="secretstore",
        )

    @pytest.mark.asyncio
    async def test_direct_multi_key_mode(self) -> None:
        """DIRECT source + multi-key bundle → bundle merged into config."""
        config = {"credentialSource": "direct", "host": "db.example.com"}
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(),
            secret_data={"password": "secret_pass"},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        assert result["host"] == "db.example.com"
        assert result["password"] == "secret_pass"

    @pytest.mark.asyncio
    async def test_missing_config_returns_empty_dict(self) -> None:
        """No config bytes → get_credentials returns empty dict (no secrets fetched)."""
        mock_client = _make_mock_dapr_client(
            config_bytes=None,
            secret_data={},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("ghost-guid")

        assert result == {}

    @pytest.mark.asyncio
    async def test_local_env_skips_secret_fetch(self) -> None:
        """In LOCAL_ENVIRONMENT, _get_secret returns {} so no Dapr secret call is made."""
        config = {"credentialSource": "direct", "user": "admin"}
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(),
            secret_data={"password": "should_not_appear"},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "local"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        # Secret should not be fetched in local env
        mock_client.get_secret.assert_not_called()
        assert result["user"] == "admin"

    @pytest.mark.asyncio
    async def test_guid_passed_as_string_not_dict(self) -> None:
        """Regression: get_credentials must receive the GUID string directly."""
        captured: list[str] = []
        config = {"credentialSource": "direct"}
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(),
            secret_data={},
        )
        original_fetch = DaprCredentialVault._fetch_credential_config

        async def _capturing_fetch(self_: Any, guid: str) -> dict[str, Any]:
            captured.append(guid)
            return await original_fetch(self_, guid)

        with (
            patch.object(
                DaprCredentialVault, "_fetch_credential_config", _capturing_fetch
            ),
            patch("application_sdk.constants.DEPLOYMENT_NAME", "local"),
        ):
            vault = self._make_vault(mock_client)
            await vault.get_credentials("abc-123")

        assert captured == ["abc-123"], f"Expected string guid, got: {captured}"

    @pytest.mark.asyncio
    async def test_vault_error_on_binding_failure(self) -> None:
        """Dapr binding error → wrapped in CredentialVaultError."""
        mock_client = MagicMock()
        mock_client.invoke_binding.side_effect = RuntimeError("Dapr unavailable")

        vault = self._make_vault(mock_client)
        with pytest.raises(CredentialVaultError):
            await vault.get_credentials("bad-guid")
