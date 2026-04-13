"""Tests for auth strategy implementations."""

from __future__ import annotations

from application_sdk.clients.auth import AuthStrategy
from application_sdk.clients.auth_strategies.api_key import ApiKeyAuthStrategy
from application_sdk.clients.auth_strategies.basic import BasicAuthStrategy
from application_sdk.clients.auth_strategies.bearer import BearerTokenAuthStrategy
from application_sdk.clients.auth_strategies.oauth import OAuthAuthStrategy
from application_sdk.credentials.types import (
    ApiKeyCredential,
    BasicCredential,
    BearerTokenCredential,
    OAuthClientCredential,
)


class TestBasicAuthStrategy:
    def test_build_url_params_encodes_password(self):
        strategy = BasicAuthStrategy()
        cred = BasicCredential(username="user", password="p@ss w0rd!")
        params = strategy.build_url_params(cred)
        assert params == {"password": "p%40ss+w0rd%21"}

    def test_build_url_params_empty_password(self):
        strategy = BasicAuthStrategy()
        cred = BasicCredential(username="user", password="")
        params = strategy.build_url_params(cred)
        assert params == {"password": ""}

    def test_build_connect_args_empty(self):
        strategy = BasicAuthStrategy()
        cred = BasicCredential(username="user", password="secret")
        assert strategy.build_connect_args(cred) == {}

    def test_build_url_query_params_empty(self):
        strategy = BasicAuthStrategy()
        cred = BasicCredential(username="user", password="secret")
        assert strategy.build_url_query_params(cred) == {}

    def test_build_headers_returns_basic_header(self):
        strategy = BasicAuthStrategy()
        cred = BasicCredential(username="user", password="secret")
        headers = strategy.build_headers(cred)
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Basic ")

    def test_credential_type_is_basic(self):
        strategy = BasicAuthStrategy()
        assert strategy.credential_type is BasicCredential

    def test_implements_auth_strategy_protocol(self):
        assert isinstance(BasicAuthStrategy(), AuthStrategy)


class TestApiKeyAuthStrategy:
    def test_build_headers_default_header_name(self):
        strategy = ApiKeyAuthStrategy()
        cred = ApiKeyCredential(api_key="abc123")
        headers = strategy.build_headers(cred)
        assert headers == {"X-API-Key": "abc123"}

    def test_build_headers_custom_header_name(self):
        strategy = ApiKeyAuthStrategy()
        cred = ApiKeyCredential(
            api_key="abc123", header_name="Authorization", prefix="Api-Token "
        )
        headers = strategy.build_headers(cred)
        assert headers == {"Authorization": "Api-Token abc123"}

    def test_build_url_params_empty(self):
        strategy = ApiKeyAuthStrategy()
        cred = ApiKeyCredential(api_key="abc123")
        assert strategy.build_url_params(cred) == {}

    def test_build_connect_args_empty(self):
        strategy = ApiKeyAuthStrategy()
        cred = ApiKeyCredential(api_key="abc123")
        assert strategy.build_connect_args(cred) == {}

    def test_credential_type_is_api_key(self):
        assert ApiKeyAuthStrategy().credential_type is ApiKeyCredential


class TestBearerTokenAuthStrategy:
    def test_build_headers_returns_bearer(self):
        strategy = BearerTokenAuthStrategy()
        cred = BearerTokenCredential(token="eyJhbGciOi")
        headers = strategy.build_headers(cred)
        assert headers == {"Authorization": "Bearer eyJhbGciOi"}

    def test_build_url_params_empty(self):
        strategy = BearerTokenAuthStrategy()
        cred = BearerTokenCredential(token="tok")
        assert strategy.build_url_params(cred) == {}

    def test_credential_type_is_bearer(self):
        assert BearerTokenAuthStrategy().credential_type is BearerTokenCredential


class TestOAuthAuthStrategy:
    def test_build_headers_uses_access_token(self):
        strategy = OAuthAuthStrategy()
        cred = OAuthClientCredential(
            client_id="cid",
            client_secret="csec",
            token_url="https://auth.example.com/token",
            access_token="at_xyz",
        )
        headers = strategy.build_headers(cred)
        assert headers == {"Authorization": "Bearer at_xyz"}

    def test_build_url_params_returns_token(self):
        strategy = OAuthAuthStrategy()
        cred = OAuthClientCredential(
            client_id="cid",
            client_secret="csec",
            token_url="https://auth.example.com/token",
            access_token="at_xyz",
        )
        params = strategy.build_url_params(cred)
        assert params == {"token": "at_xyz"}

    def test_build_url_query_params_with_extra(self):
        strategy = OAuthAuthStrategy(url_query_params={"authenticator": "oauth"})
        cred = OAuthClientCredential(
            client_id="cid",
            client_secret="csec",
            token_url="https://auth.example.com/token",
        )
        params = strategy.build_url_query_params(cred)
        assert params == {"authenticator": "oauth"}

    def test_build_url_query_params_default_empty(self):
        strategy = OAuthAuthStrategy()
        cred = OAuthClientCredential(
            client_id="cid",
            client_secret="csec",
            token_url="https://auth.example.com/token",
        )
        assert strategy.build_url_query_params(cred) == {}

    def test_credential_type_is_oauth(self):
        assert OAuthAuthStrategy().credential_type is OAuthClientCredential
