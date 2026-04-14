"""Unit tests for CredentialResolver."""

import json

import pytest

from application_sdk.credentials.errors import (
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.credentials.ref import (
    CredentialRef,
    api_key_ref,
    basic_ref,
    legacy_credential_ref,
)
from application_sdk.credentials.resolver import CredentialResolver
from application_sdk.credentials.types import (
    ApiKeyCredential,
    BasicCredential,
    RawCredential,
)
from application_sdk.infrastructure.secrets import InMemorySecretStore


@pytest.fixture
def store():
    return InMemorySecretStore()


@pytest.fixture
def resolver(store):
    return CredentialResolver(store)


class TestNewPath:
    """Tests for the new (non-legacy) resolution path."""

    @pytest.mark.asyncio
    async def test_resolve_api_key(self, store, resolver):
        store.set("prod-key", json.dumps({"type": "api_key", "api_key": "secret123"}))
        ref = api_key_ref("prod-key")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, ApiKeyCredential)
        assert cred.api_key == "secret123"

    @pytest.mark.asyncio
    async def test_resolve_basic(self, store, resolver):
        store.set(
            "db-creds", json.dumps({"type": "basic", "username": "u", "password": "p"})
        )
        ref = basic_ref("db-creds")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, BasicCredential)
        assert cred.username == "u"

    @pytest.mark.asyncio
    async def test_type_from_data_overrides_ref_type(self, store, resolver):
        """'type' field in JSON takes precedence over ref.credential_type."""
        store.set("cred", json.dumps({"type": "api_key", "api_key": "k"}))
        # Ref says basic, but data says api_key
        ref = CredentialRef(name="cred", credential_type="basic")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, ApiKeyCredential)

    @pytest.mark.asyncio
    async def test_not_found_raises(self, resolver):
        ref = api_key_ref("nonexistent")
        with pytest.raises(CredentialNotFoundError):
            await resolver.resolve(ref)

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self, store, resolver):
        store.set("bad", "not-json")
        ref = api_key_ref("bad")
        with pytest.raises(CredentialParseError):
            await resolver.resolve(ref)

    @pytest.mark.asyncio
    async def test_unknown_type_raises(self, store, resolver):
        store.set("cred", json.dumps({"type": "unknown_type_xyz"}))
        ref = CredentialRef(name="cred", credential_type="unknown_type_xyz")
        with pytest.raises(CredentialParseError, match="No parser registered"):
            await resolver.resolve(ref)

    @pytest.mark.asyncio
    async def test_resolve_raw_returns_dict(self, store, resolver):
        data = {"type": "api_key", "api_key": "secret"}
        store.set("my-key", json.dumps(data))
        ref = api_key_ref("my-key")
        raw = await resolver.resolve_raw(ref)
        assert isinstance(raw, dict)
        assert raw["api_key"] == "secret"


def _make_vault_patches(vault_return=None, vault_side_effect=None):
    """Build mock DaprCredentialVault + DaprClient patches for resolver tests.

    DaprCredentialVault and DaprClient are lazy-imported inside _resolve_by_guid,
    so they must be patched at their source modules, not on the resolver module.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_vault = MagicMock()
    if vault_side_effect is not None:
        mock_vault.get_credentials = AsyncMock(side_effect=vault_side_effect)
    else:
        mock_vault.get_credentials = AsyncMock(return_value=vault_return or {})

    mock_dapr = MagicMock()
    mock_dapr.__enter__ = MagicMock(return_value=mock_dapr)
    mock_dapr.__exit__ = MagicMock(return_value=False)

    p_vault = patch(
        "application_sdk.infrastructure._dapr.client.DaprCredentialVault",
        MagicMock(return_value=mock_vault),
    )
    p_dapr = patch("dapr.clients.DaprClient", MagicMock(return_value=mock_dapr))
    return p_vault, p_dapr, mock_vault


class TestGuidResolutionPath:
    """Tests for the GUID-based resolution path using DaprCredentialVault.

    The resolver delegates GUID resolution to DaprCredentialVault.  These
    tests mock the vault so no real Dapr connection is required.
    """

    @pytest.mark.asyncio
    async def test_get_credentials_receives_string_not_dict(self, store, resolver):
        """Regression: resolver must pass the GUID as a plain string, not a dict."""
        from unittest.mock import MagicMock, patch

        captured: list = []
        expected_creds = {"host": "db.example.com", "port": 1025}

        async def _capture(guid):
            captured.append(guid)
            return expected_creds

        mock_vault = MagicMock()
        mock_vault.get_credentials = _capture
        mock_dapr = MagicMock()
        mock_dapr.__enter__ = MagicMock(return_value=mock_dapr)
        mock_dapr.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "application_sdk.infrastructure._dapr.client.DaprCredentialVault",
                MagicMock(return_value=mock_vault),
            ),
            patch("dapr.clients.DaprClient", MagicMock(return_value=mock_dapr)),
        ):
            ref = legacy_credential_ref("abc-123")
            raw = await resolver.resolve_raw(ref)

        assert captured == ["abc-123"], f"Expected string guid, got: {captured}"
        assert raw["host"] == "db.example.com"

    @pytest.mark.asyncio
    async def test_resolve_returns_typed_credential_for_known_type(
        self, store, resolver
    ):
        """Vault returns dict → resolver parses into typed credential."""
        p_vault, p_dapr, _ = _make_vault_patches(
            vault_return={"api_key": "secret-from-vault"}
        )
        with p_vault, p_dapr:
            ref = legacy_credential_ref("abc-123", credential_type="api_key")
            cred = await resolver.resolve(ref)

        assert isinstance(cred, ApiKeyCredential)
        assert cred.api_key == "secret-from-vault"

    @pytest.mark.asyncio
    async def test_resolve_unknown_type_returns_raw_credential(self, store, resolver):
        """Vault returns dict, unknown type → resolver wraps in RawCredential."""
        p_vault, p_dapr, _ = _make_vault_patches(
            vault_return={"host": "db.example.com", "username": "u"}
        )
        with p_vault, p_dapr:
            ref = legacy_credential_ref("abc-123")
            cred = await resolver.resolve(ref)

        assert isinstance(cred, RawCredential)
        assert cred.get("host") == "db.example.com"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["resolve_raw", "resolve"])
    async def test_vault_error_raises_credential_not_found(
        self, store, resolver, method
    ):
        """When DaprCredentialVault raises, resolver re-raises as CredentialNotFoundError.

        Verifies the error does NOT silently fall through to the v3 store.
        Tests both resolve_raw() and resolve() paths.
        """
        p_vault, p_dapr, _ = _make_vault_patches(
            vault_side_effect=RuntimeError("Dapr state store unavailable")
        )
        with p_vault, p_dapr:
            ref = legacy_credential_ref("abc-123")
            with pytest.raises(CredentialNotFoundError):
                await getattr(resolver, method)(ref)
