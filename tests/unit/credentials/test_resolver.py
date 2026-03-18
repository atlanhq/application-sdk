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


class TestLegacyPath:
    """Tests for the legacy credential_guid resolution path (v3 fallback).

    When Dapr/v2 SecretStore is unavailable (mocked as ImportError), the resolver
    falls back to the v3 store keyed by the GUID.
    """

    @pytest.mark.asyncio
    async def test_legacy_ref_with_unknown_type_returns_raw(
        self, store, resolver, monkeypatch
    ):
        monkeypatch.setitem(
            __import__("sys").modules,
            "application_sdk.services.secretstore",
            None,  # causes ImportError in the resolver
        )
        store.set("abc-123", json.dumps({"username": "u", "password": "p"}))
        ref = legacy_credential_ref("abc-123")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, RawCredential)
        assert cred.get("username") == "u"

    @pytest.mark.asyncio
    async def test_legacy_ref_with_known_type_parses(
        self, store, resolver, monkeypatch
    ):
        monkeypatch.setitem(
            __import__("sys").modules,
            "application_sdk.services.secretstore",
            None,
        )
        store.set("abc-123", json.dumps({"api_key": "secret"}))
        ref = legacy_credential_ref("abc-123", credential_type="api_key")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, ApiKeyCredential)
        assert cred.api_key == "secret"

    @pytest.mark.asyncio
    async def test_legacy_raw_returns_dict(self, store, resolver, monkeypatch):
        monkeypatch.setitem(
            __import__("sys").modules,
            "application_sdk.services.secretstore",
            None,
        )
        store.set("abc-123", json.dumps({"username": "u", "password": "p"}))
        ref = legacy_credential_ref("abc-123")
        raw = await resolver.resolve_raw(ref)
        assert isinstance(raw, dict)
        assert raw["username"] == "u"
