"""Unit tests for CredentialVault and DaprCredentialVault."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.infrastructure._dapr.credential_vault import (
    DaprCredentialVault,
    _resolve_credentials,
)
from application_sdk.infrastructure._secret_utils import (
    process_secret_data as _process_secret_data,
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
    """Build a mock AsyncDaprClient that returns *config_bytes* on invoke_binding
    and *secret_data* on get_secret."""
    from application_sdk.infrastructure._dapr.http import BindingResult

    mock_client = MagicMock()

    # Binding response — AsyncDaprClient returns BindingResult (pydantic model)
    mock_client.invoke_binding = AsyncMock(
        return_value=BindingResult(data=config_bytes, metadata={})
    )

    # Secret response — AsyncDaprClient returns dict directly
    mock_client.get_secret = AsyncMock(return_value=secret_data)

    return mock_client


class TestDaprCredentialVaultGetCredentials:
    """Tests for DaprCredentialVault.get_credentials with mocked Dapr."""

    def _make_vault(self, mock_client: MagicMock) -> DaprCredentialVault:
        return DaprCredentialVault(
            mock_client,
            upstream_binding_name="upstream-objectstore",
            secret_store_name="secretstore",
        )

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

    async def test_missing_config_raises_credential_vault_error(self) -> None:
        """No config bytes → _fetch_credential_config raises CredentialVaultError."""
        mock_client = _make_mock_dapr_client(
            config_bytes=None,
            secret_data={},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            with pytest.raises(CredentialVaultError):
                await vault.get_credentials("ghost-guid")

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

    async def test_vault_error_on_binding_failure(self) -> None:
        """Dapr binding error → wrapped in CredentialVaultError."""
        mock_client = MagicMock()
        mock_client.invoke_binding = AsyncMock(
            side_effect=RuntimeError("Dapr unavailable")
        )

        vault = self._make_vault(mock_client)
        with pytest.raises(CredentialVaultError):
            await vault.get_credentials("bad-guid")

    async def test_agent_multi_key_mode_with_secret_path(self) -> None:
        """AGENT source + secret-path → fetches multi-key bundle via secret-path key."""
        config = {
            "credentialSource": "agent",
            "secret-path": "apps/myapp/creds/my-secret",
            "host": "host_ref",
        }
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(),
            secret_data={"host_ref": "db.example.com"},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        # AGENT mode: _resolve_credentials substitutes host_ref → db.example.com
        assert result["host"] == "db.example.com"
        # Secret was fetched with the secret-path key, not the GUID
        mock_client.get_secret.assert_called_once_with(
            store_name="secretstore",
            key="apps/myapp/creds/my-secret",
        )

    async def test_agent_single_key_mode(self) -> None:
        """AGENT source without secret-path → single-key mode (one lookup per field).

        In single-key mode each config field value is used as a secret key.
        _fetch_single_key_secrets collects results keyed by the secret's own key
        names so that _resolve_credentials can substitute config field values that
        equal a collected key.  The secret store must therefore return the resolved
        value under the same key that appears as the config field's value.
        """
        config = {
            "credentialSource": "agent",
            "password": "secret_key_ref",
        }
        # Secret is stored as {"secret_key_ref": "actual_password"} so that
        # _resolve_credentials can match config["password"] == "secret_key_ref".
        mock_client = _make_mock_dapr_client(
            config_bytes=json.dumps(config).encode(),
            secret_data={"secret_key_ref": "actual_password"},
        )
        with patch("application_sdk.constants.DEPLOYMENT_NAME", "production"):
            vault = self._make_vault(mock_client)
            result = await vault.get_credentials("my-guid")

        assert result.get("password") == "actual_password"

    async def test_guid_validation_rejects_path_traversal(self) -> None:
        """GUIDs with unsafe characters (e.g. '../') raise CredentialVaultError immediately."""
        mock_client = _make_mock_dapr_client(config_bytes=b"", secret_data={})
        vault = self._make_vault(mock_client)

        with pytest.raises(CredentialVaultError, match="Invalid credential GUID"):
            await vault.get_credentials("../../etc/passwd")

    async def test_guid_validation_rejects_slash(self) -> None:
        """Slashes in GUIDs are rejected before any network call."""
        mock_client = _make_mock_dapr_client(config_bytes=b"", secret_data={})
        vault = self._make_vault(mock_client)

        with pytest.raises(CredentialVaultError, match="Invalid credential GUID"):
            await vault.get_credentials("valid-prefix/injected")

        mock_client.invoke_binding.assert_not_called()
