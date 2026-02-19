"""Edge case tests for credential protocols.

Tests cover:
1. Token exchange refresh failures
2. Expired token handling
3. Missing required fields
4. Invalid configurations
5. Protocol validation edge cases

These tests ensure proper error handling and fail-fast behavior.
"""

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from application_sdk.credentials import AuthMode, Credential
from application_sdk.credentials.exceptions import CredentialRefreshError
from application_sdk.credentials.protocols.certificate import CertificateProtocol
from application_sdk.credentials.protocols.connection import ConnectionProtocol
from application_sdk.credentials.protocols.identity_pair import IdentityPairProtocol
from application_sdk.credentials.protocols.request_signing import RequestSigningProtocol
from application_sdk.credentials.protocols.static_secret import StaticSecretProtocol
from application_sdk.credentials.protocols.token_exchange import TokenExchangeProtocol
from application_sdk.credentials.resolver import CredentialResolver


class TestTokenExchangeEdgeCases:
    """Test TOKEN_EXCHANGE protocol edge cases and error handling."""

    def test_token_refresh_fails_fast_when_refresh_fails(self):
        """Token refresh failure should raise error immediately (fail-fast).

        This ensures authentication failures are surfaced immediately rather
        than being masked by returning potentially expired tokens.
        """
        protocol = TokenExchangeProtocol(
            config={
                "token_url": "https://auth.example.com/oauth/token",
                "grant_type": "client_credentials",
            }
        )

        credentials = {
            "client_id": "test-client",
            "client_secret": "test-secret",
            "access_token": "expired_token",
            "token_expiry": time.time() - 3600,  # Expired 1 hour ago
        }

        with patch.object(
            protocol,
            "refresh",
            side_effect=CredentialRefreshError("Token refresh failed"),
        ):
            with pytest.raises(CredentialRefreshError, match="Token refresh failed"):
                protocol._get_valid_token(credentials)

    def test_missing_token_url_raises_error(self):
        """Missing token_url should raise clear error."""
        protocol = TokenExchangeProtocol(
            config={
                "grant_type": "client_credentials",
            }
        )

        credentials = {
            "client_id": "test-client",
            "client_secret": "test-secret",
        }

        with pytest.raises(CredentialRefreshError, match="No token_url configured"):
            protocol.refresh(credentials)

    def test_valid_token_is_returned_without_refresh(self):
        """Valid non-expired token should be returned directly."""
        protocol = TokenExchangeProtocol(
            config={
                "token_url": "https://auth.example.com/oauth/token",
                "token_expiry_buffer": 60,
            }
        )

        # Token expires in 2 hours (well within buffer)
        credentials = {
            "client_id": "test-client",
            "client_secret": "test-secret",
            "access_token": "valid_token",
            "token_expiry": time.time() + 7200,
        }

        # Should return token without calling refresh
        with patch.object(protocol, "refresh") as mock_refresh:
            token = protocol._get_valid_token(credentials)
            assert token == "valid_token"
            mock_refresh.assert_not_called()

    def test_token_near_expiry_triggers_refresh(self):
        """Token near expiry (within buffer) should trigger refresh."""
        protocol = TokenExchangeProtocol(
            config={
                "token_url": "https://auth.example.com/oauth/token",
                "token_expiry_buffer": 120,  # 2 minute buffer
            }
        )

        # Token expires in 60 seconds (within 120s buffer)
        credentials = {
            "client_id": "test-client",
            "client_secret": "test-secret",
            "access_token": "expiring_token",
            "token_expiry": time.time() + 60,
        }

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "new_token",
            "expires_in": 3600,
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_response):
            token = protocol._get_valid_token(credentials)
            assert token == "new_token"

    def test_refresh_token_flow_fallback_to_reauth(self):
        """When refresh token fails, should fall back to re-authentication."""
        protocol = TokenExchangeProtocol(
            config={
                "token_url": "https://auth.example.com/oauth/token",
                "grant_type": "client_credentials",
            }
        )

        credentials = {
            "client_id": "test-client",
            "client_secret": "test-secret",
            "refresh_token": "expired_refresh_token",
        }

        # First call (refresh) fails, second call (re-auth) succeeds
        def side_effect(*args, **kwargs):
            data = kwargs.get("data", {})
            if data.get("grant_type") == "refresh_token":
                raise httpx.HTTPStatusError(
                    "Refresh failed",
                    request=MagicMock(),
                    response=MagicMock(status_code=401),
                )
            else:
                response = MagicMock()
                response.json.return_value = {
                    "access_token": "new_token_from_reauth",
                    "expires_in": 3600,
                }
                response.raise_for_status = MagicMock()
                return response

        with patch("httpx.post", side_effect=side_effect):
            result = protocol.refresh(credentials)
            assert result["access_token"] == "new_token_from_reauth"

    def test_validation_requires_client_credentials(self):
        """Validation should fail without client_id/client_secret."""
        protocol = TokenExchangeProtocol(
            config={
                "token_url": "https://auth.example.com/oauth/token",
            }
        )

        # Missing client_id
        result = protocol.validate({"client_secret": "secret"})
        assert not result.valid
        assert "client_id is required" in result.errors

        # Missing client_secret
        result = protocol.validate({"client_id": "client"})
        assert not result.valid
        assert "client_secret is required" in result.errors

        # Missing both
        result = protocol.validate({})
        assert not result.valid
        assert len(result.errors) >= 2

    def test_validation_accepts_existing_access_token(self):
        """Validation should pass with existing access_token (no token_url needed)."""
        protocol = TokenExchangeProtocol(config={})

        result = protocol.validate(
            {
                "client_id": "client",
                "client_secret": "secret",
                "access_token": "existing_token",
            }
        )

        assert result.valid

    def test_http_error_wrapped_in_credential_refresh_error(self):
        """HTTP errors should be wrapped in CredentialRefreshError."""
        protocol = TokenExchangeProtocol(
            config={
                "token_url": "https://auth.example.com/oauth/token",
            }
        )

        credentials = {
            "client_id": "test-client",
            "client_secret": "test-secret",
        }

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized", request=MagicMock(), response=MagicMock(status_code=401)
        )

        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(CredentialRefreshError, match="Authentication failed"):
                protocol._authenticate(credentials)


class TestStaticSecretEdgeCases:
    """Test STATIC_SECRET protocol edge cases."""

    def test_missing_api_key_returns_empty_result(self):
        """Missing api_key should return empty ApplyResult."""
        protocol = StaticSecretProtocol(
            config={
                "location": "header",
                "header_name": "Authorization",
                "prefix": "Bearer ",
            }
        )

        result = protocol.apply({}, {})

        assert result.headers == {}
        assert result.query_params == {}

    def test_field_aliases_work(self):
        """Field aliases (secret_key, token, etc.) should work."""
        protocol = StaticSecretProtocol(
            config={
                "location": "header",
                "header_name": "Authorization",
                "prefix": "Bearer ",
            }
        )

        # secret_key alias
        result = protocol.apply({"secret_key": "my_secret"}, {})
        assert result.headers == {"Authorization": "Bearer my_secret"}

        # token alias
        result = protocol.apply({"token": "my_token"}, {})
        assert result.headers == {"Authorization": "Bearer my_token"}

    def test_query_param_location(self):
        """Query parameter location should work correctly."""
        protocol = StaticSecretProtocol(
            config={
                "location": "query",
                "param_name": "api_key",
            }
        )

        result = protocol.apply({"api_key": "test_key"}, {})

        assert result.query_params == {"api_key": "test_key"}
        assert result.headers == {}

    def test_validation_requires_api_key(self):
        """Validation should fail without api_key."""
        protocol = StaticSecretProtocol(config={})

        result = protocol.validate({})
        assert not result.valid
        assert any(
            "api_key" in err.lower() or "required" in err.lower()
            for err in result.errors
        )


class TestIdentityPairEdgeCases:
    """Test IDENTITY_PAIR protocol edge cases."""

    def test_missing_identity_returns_empty_result(self):
        """Missing identity field should return empty result."""
        protocol = IdentityPairProtocol(
            config={
                "encoding": "basic",
                "identity_field": "username",
                "secret_field": "password",
            }
        )

        result = protocol.apply({"password": "pass"}, {})

        # Should handle gracefully
        assert result.headers == {} or "Authorization" not in result.headers

    def test_header_pair_location(self):
        """Header pair location should set two headers."""
        protocol = IdentityPairProtocol(
            config={
                "location": "header_pair",
                "identity_field": "client_id",
                "secret_field": "secret",
                "identity_header": "X-Client-ID",
                "secret_header": "X-Client-Secret",
            }
        )

        result = protocol.apply({"client_id": "abc", "secret": "xyz"}, {})

        assert result.headers == {
            "X-Client-ID": "abc",
            "X-Client-Secret": "xyz",
        }

    def test_body_location(self):
        """Body location should set credentials in body."""
        protocol = IdentityPairProtocol(
            config={
                "location": "body",
                "identity_field": "client_id",
                "secret_field": "secret",
            }
        )

        result = protocol.apply({"client_id": "abc", "secret": "xyz"}, {})

        assert result.body == {"client_id": "abc", "secret": "xyz"}


class TestRequestSigningEdgeCases:
    """Test REQUEST_SIGNING protocol edge cases."""

    def test_missing_credentials_validation(self):
        """Validation should fail without required signing credentials."""
        protocol = RequestSigningProtocol(
            config={
                "algorithm": "aws_sigv4",
            }
        )

        result = protocol.validate({})
        assert not result.valid


class TestCertificateEdgeCases:
    """Test CERTIFICATE protocol edge cases."""

    def test_missing_certificate_validation(self):
        """Validation should fail without certificate."""
        protocol = CertificateProtocol(config={})

        result = protocol.validate({})
        assert not result.valid


class TestConnectionEdgeCases:
    """Test CONNECTION protocol edge cases."""

    def test_returns_all_credentials_in_materialize(self):
        """Materialize should return all connection credentials."""
        protocol = ConnectionProtocol(config={})

        credentials = {
            "host": "localhost",
            "port": 5432,
            "database": "mydb",
            "username": "user",
            "password": "secret",
        }

        result = protocol.materialize(credentials)

        # Should contain all fields
        assert "host" in result.credentials
        assert "database" in result.credentials


class TestCredentialResolverEdgeCases:
    """Test CredentialResolver edge cases."""

    def test_custom_auth_mode_without_protocol_raises(self):
        """CUSTOM auth mode without protocol instance should raise."""
        cred = Credential(name="test", auth=AuthMode.CUSTOM)

        with pytest.raises(ValueError, match="CUSTOM requires a protocol instance"):
            CredentialResolver.resolve(cred)

    def test_direct_protocol_instance_escape_hatch(self):
        """Direct protocol instance should be returned as-is."""
        custom_protocol = StaticSecretProtocol(config={"custom": "config"})
        cred = Credential(name="test", protocol=custom_protocol)

        resolved = CredentialResolver.resolve(cred)

        assert resolved is custom_protocol

    def test_all_auth_modes_have_protocol_mapping(self):
        """All AuthModes (except CUSTOM) should have a protocol mapping."""
        from application_sdk.credentials.resolver import AUTH_MODE_TO_PROTOCOL
        from application_sdk.credentials.types import AuthMode

        for mode in AuthMode:
            if mode != AuthMode.CUSTOM:
                assert mode in AUTH_MODE_TO_PROTOCOL, f"Missing mapping for {mode}"

    def test_config_override_merges_with_defaults(self):
        """config_override should merge with AUTH_MODE_DEFAULTS."""
        cred = Credential(
            name="test",
            auth=AuthMode.API_KEY,
            config_override={"custom_key": "custom_value"},
        )

        protocol = CredentialResolver.resolve(cred)

        # Should have default config
        assert protocol.config.get("header_name") == "Authorization"
        assert protocol.config.get("prefix") == "Bearer "
        # Plus override
        assert protocol.config.get("custom_key") == "custom_value"


class TestNewAuthModes:
    """Test newly added auth modes work correctly."""

    def test_api_key_header_mode(self):
        """API_KEY_HEADER should use X-API-Key header without prefix."""
        cred = Credential(name="datadog", auth=AuthMode.API_KEY_HEADER)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "dd-api-key-123"}, {})

        assert result.headers == {"X-API-Key": "dd-api-key-123"}
        assert protocol.config.get("prefix") == ""

    def test_service_token_mode(self):
        """SERVICE_TOKEN should use Authorization header without prefix."""
        cred = Credential(name="internal", auth=AuthMode.SERVICE_TOKEN)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "raw_service_token"}, {})

        assert result.headers == {"Authorization": "raw_service_token"}
        assert protocol.config.get("prefix") == ""

    def test_atlan_api_key_mode(self):
        """ATLAN_API_KEY should use Bearer prefix."""
        cred = Credential(name="atlan", auth=AuthMode.ATLAN_API_KEY)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply({"api_key": "atlan-api-key"}, {})

        assert result.headers == {"Authorization": "Bearer atlan-api-key"}

    def test_atlan_oauth_mode(self):
        """ATLAN_OAUTH should use client_credentials grant."""
        cred = Credential(name="atlan_oauth", auth=AuthMode.ATLAN_OAUTH)
        protocol = CredentialResolver.resolve(cred)

        assert protocol.config.get("grant_type") == "client_credentials"
        assert protocol.config.get("token_endpoint_auth_method") == "client_secret_post"

    def test_api_key_secret_mode(self):
        """API_KEY_SECRET should use two headers for key+secret pair."""
        cred = Credential(name="s3_compatible", auth=AuthMode.API_KEY_SECRET)
        protocol = CredentialResolver.resolve(cred)

        result = protocol.apply(
            {"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "secret123"},
            {},
        )

        assert result.headers == {
            "X-Access-Key-Id": "AKIAIOSFODNN7EXAMPLE",
            "X-Secret-Access-Key": "secret123",
        }

    def test_digest_auth_mode(self):
        """DIGEST_AUTH should use digest encoding."""
        cred = Credential(name="enterprise_api", auth=AuthMode.DIGEST_AUTH)
        protocol = CredentialResolver.resolve(cred)

        assert protocol.config.get("encoding") == "digest"
        assert protocol.config.get("identity_field") == "username"
        assert protocol.config.get("secret_field") == "password"

    def test_oauth2_password_mode(self):
        """OAUTH2_PASSWORD should use password grant type."""
        cred = Credential(name="legacy_api", auth=AuthMode.OAUTH2_PASSWORD)
        protocol = CredentialResolver.resolve(cred)

        assert protocol.config.get("grant_type") == "password"


class TestCredentialConfigFields:
    """Test new Credential configuration fields."""

    def test_default_retry_status_codes(self):
        """Default retry_status_codes should be {401, 403}."""
        cred = Credential(name="test", auth=AuthMode.API_KEY)

        assert cred.retry_status_codes == {401, 403}

    def test_custom_retry_status_codes(self):
        """Custom retry_status_codes should include additional codes."""
        cred = Credential(
            name="test",
            auth=AuthMode.API_KEY,
            retry_status_codes={401, 403, 429, 500, 502, 503},
        )

        assert 429 in cred.retry_status_codes
        assert 500 in cred.retry_status_codes

    def test_default_max_retries(self):
        """Default max_retries should be 3."""
        cred = Credential(name="test", auth=AuthMode.API_KEY)

        assert cred.max_retries == 3

    def test_custom_max_retries(self):
        """Custom max_retries should be settable."""
        cred = Credential(name="test", auth=AuthMode.API_KEY, max_retries=5)

        assert cred.max_retries == 5

    def test_default_token_expiry_buffer(self):
        """Default token_expiry_buffer should be 60 seconds."""
        cred = Credential(name="test", auth=AuthMode.OAUTH2_CLIENT_CREDENTIALS)

        assert cred.token_expiry_buffer == 60

    def test_custom_token_expiry_buffer(self):
        """Custom token_expiry_buffer for slow token endpoints."""
        cred = Credential(
            name="slow_oauth",
            auth=AuthMode.OAUTH2_CLIENT_CREDENTIALS,
            token_expiry_buffer=300,  # 5 minutes for slow endpoints
        )

        assert cred.token_expiry_buffer == 300

    def test_to_dict_includes_custom_retry_codes(self):
        """to_dict should include non-default retry_status_codes."""
        cred = Credential(
            name="test",
            auth=AuthMode.API_KEY,
            retry_status_codes={401, 403, 429},
        )

        result = cred.to_dict()

        assert "retry_status_codes" in result
        assert set(result["retry_status_codes"]) == {401, 403, 429}

    def test_to_dict_excludes_default_values(self):
        """to_dict should exclude default values to reduce noise."""
        cred = Credential(name="test", auth=AuthMode.API_KEY)

        result = cred.to_dict()

        # Defaults should not be in output
        assert "retry_status_codes" not in result
        assert "token_expiry_buffer" not in result
        assert "max_retries" not in result
