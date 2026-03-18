"""Unit tests for CredentialTypeRegistry."""

import pytest

from application_sdk.credentials.errors import CredentialParseError
from application_sdk.credentials.registry import CredentialTypeRegistry, get_registry
from application_sdk.credentials.types import (
    ApiKeyCredential,
    BasicCredential,
    BearerTokenCredential,
    CertificateCredential,
    OAuthClientCredential,
)


@pytest.fixture
def registry():
    """Return a fresh registry for each test (reset the singleton)."""
    reg = CredentialTypeRegistry()
    # Reset internal state to ensure builtins are re-registered cleanly
    reg._registry = {}
    reg._initialized = False
    return reg


class TestBuiltinRegistration:
    def test_all_builtins_registered(self, registry):
        types = registry.registered_types()
        expected = [
            "api_key",
            "atlan_api_token",
            "atlan_oauth_client",
            "basic",
            "bearer_token",
            "certificate",
            "git_ssh",
            "git_token",
            "oauth_client",
        ]
        for t in expected:
            assert t in types, f"Expected '{t}' to be registered"

    def test_parse_basic(self, registry):
        cred = registry.parse("basic", {"username": "user", "password": "pass"})
        assert isinstance(cred, BasicCredential)
        assert cred.username == "user"

    def test_parse_api_key(self, registry):
        cred = registry.parse("api_key", {"api_key": "secret"})
        assert isinstance(cred, ApiKeyCredential)
        assert cred.api_key == "secret"

    def test_parse_bearer_token(self, registry):
        cred = registry.parse("bearer_token", {"token": "tok"})
        assert isinstance(cred, BearerTokenCredential)

    def test_parse_oauth_client(self, registry):
        cred = registry.parse(
            "oauth_client",
            {
                "client_id": "cid",
                "client_secret": "csec",
                "token_url": "https://auth.example.com/token",
            },
        )
        assert isinstance(cred, OAuthClientCredential)

    def test_parse_certificate(self, registry):
        cred = registry.parse("certificate", {"key_data": "pem-key"})
        assert isinstance(cred, CertificateCredential)


class TestCustomRegistration:
    def test_register_custom_type(self, registry):
        import dataclasses

        @dataclasses.dataclass(frozen=True)
        class MyCredential:
            token: str

            @property
            def credential_type(self) -> str:
                return "my_custom"

            async def validate(self) -> None:
                pass

        def _parse_my(data):
            return MyCredential(token=data.get("token", ""))

        registry.register_credential_type("my_custom", MyCredential, _parse_my)
        cred = registry.parse("my_custom", {"token": "abc"})
        assert isinstance(cred, MyCredential)
        assert cred.token == "abc"

    def test_unknown_type_raises(self, registry):
        with pytest.raises(CredentialParseError, match="No parser registered"):
            registry.parse("nonexistent_type", {})

    def test_get_class(self, registry):
        cls = registry.get_class("basic")
        assert cls is BasicCredential

    def test_get_class_unknown(self, registry):
        assert registry.get_class("nonexistent") is None


class TestSingleton:
    def test_singleton_identity(self):
        a = get_registry()
        b = get_registry()
        assert a is b
