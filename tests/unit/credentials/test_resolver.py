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


class TestLegacyV2SecretStorePath:
    """Tests for the legacy path when v2 SecretStore IS available.

    Verifies that _resolve_legacy_raw passes the credential_guid as a bare
    string to V2SecretStore.get_credentials, not wrapped in a dict.
    """

    @pytest.mark.asyncio
    async def test_get_credentials_receives_string_not_dict(
        self, store, resolver, monkeypatch
    ):
        """The resolver must pass credential_guid as a string to get_credentials.

        Regression test: previously passed {"credential_guid": guid} (a dict),
        which caused StateStore.get_state to miss the credential lookup.
        """
        captured_args = []
        expected_creds = {
            "host": "db.example.com",
            "port": 1025,
            "username": "u",
            "password": "p",
        }

        class FakeV2SecretStore:
            @classmethod
            async def get_credentials(cls, credential_guid):
                captured_args.append(credential_guid)
                return expected_creds

        # Inject fake v2 SecretStore module
        import types

        fake_module = types.ModuleType("application_sdk.services.secretstore")
        fake_module.SecretStore = FakeV2SecretStore
        monkeypatch.setitem(
            __import__("sys").modules,
            "application_sdk.services.secretstore",
            fake_module,
        )

        ref = legacy_credential_ref("abc-123")
        raw = await resolver.resolve_raw(ref)

        assert len(captured_args) == 1
        assert (
            captured_args[0] == "abc-123"
        ), f"get_credentials should receive a string, got {type(captured_args[0])}: {captured_args[0]}"
        assert isinstance(raw, dict)
        assert raw["host"] == "db.example.com"

    @pytest.mark.asyncio
    async def test_resolve_legacy_returns_typed_credential(
        self, store, resolver, monkeypatch
    ):
        """Legacy path with v2 SecretStore returns typed credential when type is known."""
        import types

        class FakeV2SecretStore:
            @classmethod
            async def get_credentials(cls, credential_guid):
                return {"api_key": "secret-from-heracles"}

        fake_module = types.ModuleType("application_sdk.services.secretstore")
        fake_module.SecretStore = FakeV2SecretStore
        monkeypatch.setitem(
            __import__("sys").modules,
            "application_sdk.services.secretstore",
            fake_module,
        )

        ref = legacy_credential_ref("abc-123", credential_type="api_key")
        cred = await resolver.resolve(ref)

        assert isinstance(cred, ApiKeyCredential)
        assert cred.api_key == "secret-from-heracles"

    @pytest.mark.asyncio
    async def test_resolve_legacy_unknown_type_returns_raw(
        self, store, resolver, monkeypatch
    ):
        """Legacy path with v2 SecretStore wraps in RawCredential for unknown type."""
        import types

        class FakeV2SecretStore:
            @classmethod
            async def get_credentials(cls, credential_guid):
                return {"host": "db.example.com", "username": "u", "password": "p"}

        fake_module = types.ModuleType("application_sdk.services.secretstore")
        fake_module.SecretStore = FakeV2SecretStore
        monkeypatch.setitem(
            __import__("sys").modules,
            "application_sdk.services.secretstore",
            fake_module,
        )

        ref = legacy_credential_ref("abc-123")  # credential_type="unknown"
        cred = await resolver.resolve(ref)

        assert isinstance(cred, RawCredential)
        assert cred.get("host") == "db.example.com"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["resolve_raw", "resolve"])
    async def test_v2_store_error_raises_credential_not_found(
        self, store, resolver, monkeypatch, method
    ):
        """When v2 SecretStore raises, resolver re-raises as CredentialNotFoundError.

        This verifies the error does NOT silently fall through to the v3 store.
        Tests both resolve_raw() and resolve() paths.
        """
        import types

        class FakeV2SecretStore:
            @classmethod
            async def get_credentials(cls, credential_guid):
                raise RuntimeError("Dapr state store unavailable")

        fake_module = types.ModuleType("application_sdk.services.secretstore")
        fake_module.SecretStore = FakeV2SecretStore
        monkeypatch.setitem(
            __import__("sys").modules,
            "application_sdk.services.secretstore",
            fake_module,
        )

        ref = legacy_credential_ref("abc-123")
        with pytest.raises(CredentialNotFoundError):
            await getattr(resolver, method)(ref)
