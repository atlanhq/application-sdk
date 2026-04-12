"""Unit tests for MockCredentialStore."""

import pytest

from application_sdk.credentials.atlan import AtlanApiToken, AtlanOAuthClient
from application_sdk.credentials.git import GitSshCredential, GitTokenCredential
from application_sdk.credentials.resolver import CredentialResolver
from application_sdk.credentials.types import (
    ApiKeyCredential,
    BasicCredential,
    BearerTokenCredential,
    CertificateCredential,
    OAuthClientCredential,
)
from application_sdk.testing import MockCredentialStore


@pytest.fixture
def mock_store():
    return MockCredentialStore()


@pytest.fixture
def resolver(mock_store):
    return CredentialResolver(mock_store.secret_store)


class TestMockCredentialStore:
    @pytest.mark.asyncio
    async def test_add_api_key(self, mock_store, resolver):
        ref = mock_store.add_api_key("my-key", api_key="secret")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, ApiKeyCredential)
        assert cred.api_key == "secret"

    @pytest.mark.asyncio
    async def test_add_api_key_custom_header(self, mock_store, resolver):
        ref = mock_store.add_api_key("my-key", api_key="k", header_name="X-Token")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, ApiKeyCredential)
        assert cred.header_name == "X-Token"

    @pytest.mark.asyncio
    async def test_add_basic(self, mock_store, resolver):
        ref = mock_store.add_basic("db", "user", "pass")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, BasicCredential)
        assert cred.username == "user"
        assert cred.password == "pass"

    @pytest.mark.asyncio
    async def test_add_bearer_token(self, mock_store, resolver):
        ref = mock_store.add_bearer_token("svc", "tok123")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, BearerTokenCredential)
        assert cred.token == "tok123"

    @pytest.mark.asyncio
    async def test_add_oauth_client(self, mock_store, resolver):
        ref = mock_store.add_oauth_client(
            "oauth",
            "cid",
            "csec",
            "https://auth.example.com/token",
            scopes=["read", "write"],
        )
        cred = await resolver.resolve(ref)
        assert isinstance(cred, OAuthClientCredential)
        assert cred.client_id == "cid"
        assert "read" in cred.scopes

    @pytest.mark.asyncio
    async def test_add_certificate(self, mock_store, resolver):
        ref = mock_store.add_certificate("tls", cert_data="cert", key_data="key")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, CertificateCredential)
        assert cred.cert_data == "cert"

    @pytest.mark.asyncio
    async def test_add_git_ssh(self, mock_store, resolver):
        ref = mock_store.add_git_ssh("git", "ssh-private-key")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, GitSshCredential)
        assert cred.key_data == "ssh-private-key"

    @pytest.mark.asyncio
    async def test_add_git_token(self, mock_store, resolver):
        ref = mock_store.add_git_token("pat", "ghp_token")
        cred = await resolver.resolve(ref)
        assert isinstance(cred, GitTokenCredential)
        assert cred.token == "ghp_token"

    @pytest.mark.asyncio
    async def test_add_atlan_api_token(self, mock_store, resolver):
        ref = mock_store.add_atlan_api_token(
            "atlan", "api-tok", "https://tenant.atlan.com"
        )
        cred = await resolver.resolve(ref)
        assert isinstance(cred, AtlanApiToken)
        assert cred.token == "api-tok"
        assert cred.base_url == "https://tenant.atlan.com"

    @pytest.mark.asyncio
    async def test_add_atlan_oauth_client(self, mock_store, resolver):
        ref = mock_store.add_atlan_oauth_client(
            "atlan-oauth",
            "cid",
            "csec",
            "https://tenant.atlan.com",
            access_token="tok",
        )
        cred = await resolver.resolve(ref)
        assert isinstance(cred, AtlanOAuthClient)
        assert cred.client_id == "cid"
        assert cred.base_url == "https://tenant.atlan.com"

    @pytest.mark.asyncio
    async def test_add_raw(self, mock_store):
        """add_raw stores arbitrary data that can be fetched via resolve_raw."""
        ref = mock_store.add_raw("custom", "my_custom_type", {"foo": "bar"})
        # Use resolve_raw instead of resolve since "my_custom_type" has no parser
        resolver = CredentialResolver(mock_store.secret_store)
        raw = await resolver.resolve_raw(ref)
        assert isinstance(raw, dict)
        assert raw["foo"] == "bar"

    def test_ref_returned_matches_name(self, mock_store):
        ref = mock_store.add_basic("my-creds", "u", "p")
        assert ref.name == "my-creds"

    def test_secret_store_property(self, mock_store):
        from application_sdk.infrastructure.secrets import InMemorySecretStore

        assert isinstance(mock_store.secret_store, InMemorySecretStore)
