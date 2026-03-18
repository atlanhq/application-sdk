"""Unit tests for built-in credential types."""

import pytest

from application_sdk.credentials.errors import CredentialValidationError
from application_sdk.credentials.types import (
    ApiKeyCredential,
    BasicCredential,
    BearerTokenCredential,
    CertificateCredential,
    OAuthClientCredential,
    RawCredential,
)


class TestBasicCredential:
    def test_credential_type(self):
        cred = BasicCredential(username="user", password="pass")
        assert cred.credential_type == "basic"

    def test_to_auth_header(self):
        import base64

        cred = BasicCredential(username="user", password="pass")
        header = cred.to_auth_header()
        assert header.startswith("Basic ")
        decoded = base64.b64decode(header[6:]).decode()
        assert decoded == "user:pass"

    @pytest.mark.asyncio
    async def test_validate_ok(self):
        cred = BasicCredential(username="user", password="pass")
        await cred.validate()  # should not raise

    @pytest.mark.asyncio
    async def test_validate_empty_username(self):
        cred = BasicCredential(username="", password="pass")
        with pytest.raises(CredentialValidationError):
            await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_empty_password(self):
        cred = BasicCredential(username="user", password="")
        with pytest.raises(CredentialValidationError):
            await cred.validate()

    def test_frozen(self):
        import dataclasses

        cred = BasicCredential(username="u", password="p")
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            cred.username = "x"  # type: ignore[misc]


class TestApiKeyCredential:
    def test_credential_type(self):
        cred = ApiKeyCredential(api_key="secret")
        assert cred.credential_type == "api_key"

    def test_to_headers_default(self):
        cred = ApiKeyCredential(api_key="my-key")
        assert cred.to_headers() == {"X-API-Key": "my-key"}

    def test_to_headers_custom_name(self):
        cred = ApiKeyCredential(api_key="key", header_name="Authorization")
        assert cred.to_headers() == {"Authorization": "key"}

    def test_to_headers_with_prefix(self):
        cred = ApiKeyCredential(
            api_key="key", header_name="Authorization", prefix="Bearer "
        )
        assert cred.to_headers() == {"Authorization": "Bearer key"}

    @pytest.mark.asyncio
    async def test_validate_ok(self):
        cred = ApiKeyCredential(api_key="secret")
        await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_empty(self):
        cred = ApiKeyCredential(api_key="")
        with pytest.raises(CredentialValidationError):
            await cred.validate()


class TestBearerTokenCredential:
    def test_credential_type(self):
        cred = BearerTokenCredential(token="tok")
        assert cred.credential_type == "bearer_token"

    def test_to_auth_header(self):
        cred = BearerTokenCredential(token="mytoken")
        assert cred.to_auth_header() == "Bearer mytoken"

    def test_is_expired_no_expiry(self):
        cred = BearerTokenCredential(token="tok")
        assert cred.is_expired() is False

    def test_is_expired_future(self):
        cred = BearerTokenCredential(token="tok", expires_at="2099-01-01T00:00:00Z")
        assert cred.is_expired() is False

    def test_is_expired_past(self):
        cred = BearerTokenCredential(token="tok", expires_at="2000-01-01T00:00:00Z")
        assert cred.is_expired() is True

    @pytest.mark.asyncio
    async def test_validate_ok(self):
        cred = BearerTokenCredential(token="tok")
        await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_empty(self):
        cred = BearerTokenCredential(token="")
        with pytest.raises(CredentialValidationError):
            await cred.validate()


class TestOAuthClientCredential:
    def _make(self, **kwargs):
        defaults = dict(
            client_id="cid",
            client_secret="csec",
            token_url="https://auth.example.com/token",
        )
        defaults.update(kwargs)
        return OAuthClientCredential(**defaults)

    def test_credential_type(self):
        assert self._make().credential_type == "oauth_client"

    def test_needs_refresh_no_token(self):
        cred = self._make()
        assert cred.needs_refresh() is True

    def test_needs_refresh_has_valid_token(self):
        cred = self._make(access_token="tok", expires_at="2099-01-01T00:00:00Z")
        assert cred.needs_refresh() is False

    def test_needs_refresh_expired(self):
        cred = self._make(access_token="tok", expires_at="2000-01-01T00:00:00Z")
        assert cred.needs_refresh() is True

    def test_with_new_token(self):
        cred = self._make()
        updated = cred.with_new_token("newtok", expires_at="2099-01-01T00:00:00Z")
        assert updated.access_token == "newtok"
        assert updated.client_id == "cid"
        assert isinstance(updated, OAuthClientCredential)

    def test_to_headers(self):
        cred = self._make(access_token="mytoken")
        assert cred.to_headers() == {"Authorization": "Bearer mytoken"}

    @pytest.mark.asyncio
    async def test_validate_ok(self):
        await self._make().validate()

    @pytest.mark.asyncio
    async def test_validate_missing_client_id(self):
        cred = self._make(client_id="")
        with pytest.raises(CredentialValidationError):
            await cred.validate()


class TestCertificateCredential:
    def test_credential_type(self):
        cred = CertificateCredential(cert_data="cert", key_data="key")
        assert cred.credential_type == "certificate"

    @pytest.mark.asyncio
    async def test_validate_ok_with_key(self):
        cred = CertificateCredential(key_data="key")
        await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_ok_with_cert(self):
        cred = CertificateCredential(cert_data="cert")
        await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_empty(self):
        cred = CertificateCredential()
        with pytest.raises(CredentialValidationError):
            await cred.validate()


class TestRawCredential:
    def test_credential_type(self):
        cred = RawCredential(data={"foo": "bar"})
        assert cred.credential_type == "raw"

    def test_get(self):
        cred = RawCredential(data={"key": "value"})
        assert cred.get("key") == "value"
        assert cred.get("missing") is None
        assert cred.get("missing", "default") == "default"

    @pytest.mark.asyncio
    async def test_validate_noop(self):
        cred = RawCredential(data={})
        await cred.validate()  # should not raise
