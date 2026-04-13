"""Tests for Client shared auth layer."""

import pytest

from application_sdk.clients.auth_strategies.basic import BasicAuthStrategy
from application_sdk.clients.auth_strategies.oauth import OAuthAuthStrategy
from application_sdk.clients.client import Client
from application_sdk.common.error_codes import ClientError
from application_sdk.credentials.types import (
    BasicCredential,
    CertificateCredential,
    OAuthClientCredential,
)


# Client.load() raises NotImplementedError by default.
# Create a concrete subclass for testing.
class ConcreteClient(Client):
    async def load(self, **kwargs):
        pass


class ClientWithStrategies(Client):
    AUTH_STRATEGIES = {
        BasicCredential: BasicAuthStrategy(),
        OAuthClientCredential: OAuthAuthStrategy(
            url_query_params={"authenticator": "oauth"},
        ),
    }

    async def load(self, **kwargs):
        pass


class TestClientInit:
    def test_default_credentials_empty(self):
        client = ConcreteClient()
        assert client.credentials == {}

    def test_credentials_stored(self):
        client = ConcreteClient(credentials={"host": "localhost"})
        assert client.credentials == {"host": "localhost"}

    def test_default_auth_strategies_empty(self):
        assert ConcreteClient.AUTH_STRATEGIES == {}

    def test_subclass_strategies_not_shared(self):
        """Subclass AUTH_STRATEGIES doesn't leak to parent."""
        assert ConcreteClient.AUTH_STRATEGIES == {}
        assert len(ClientWithStrategies.AUTH_STRATEGIES) == 2


class TestResolveStrategy:
    def test_resolve_registered_credential(self):
        client = ClientWithStrategies()
        cred = BasicCredential(username="u", password="p")
        strategy = client._resolve_strategy(cred)
        assert isinstance(strategy, BasicAuthStrategy)

    def test_resolve_unregistered_credential_raises(self):
        client = ClientWithStrategies()
        cred = CertificateCredential(key_data="pem")
        with pytest.raises(ClientError, match="No auth strategy registered"):
            client._resolve_strategy(cred)

    def test_resolve_on_empty_strategies_raises(self):
        client = ConcreteClient()
        cred = BasicCredential(username="u", password="p")
        with pytest.raises(ClientError, match="No auth strategy registered"):
            client._resolve_strategy(cred)


class TestAddUrlParams:
    def test_first_param_uses_question_mark(self):
        client = ConcreteClient()
        result = client.add_url_params("https://api.example.com", {"page": "1"})
        assert result == "https://api.example.com?page=1"

    def test_subsequent_params_use_ampersand(self):
        client = ConcreteClient()
        result = client.add_url_params("https://api.example.com?page=1", {"size": "50"})
        assert result == "https://api.example.com?page=1&size=50"

    def test_empty_params_returns_unchanged(self):
        client = ConcreteClient()
        url = "https://api.example.com"
        assert client.add_url_params(url, {}) == url

    def test_multiple_params(self):
        client = ConcreteClient()
        result = client.add_url_params(
            "postgresql://localhost/db", {"ssl": "true", "timeout": "30"}
        )
        assert "ssl=true" in result
        assert "timeout=30" in result
        assert result.count("?") == 1


class TestBuildUrl:
    def test_basic_template_formatting(self):
        client = ClientWithStrategies()
        cred = BasicCredential(username="user", password="s3cret")
        strategy = client._resolve_strategy(cred)
        url = client._build_url(
            "postgresql://{username}:{password}@localhost/db",
            strategy,
            cred,
            username="user",
        )
        assert "s3cret" in url
        assert "user" in url

    def test_defaults_appended(self):
        client = ClientWithStrategies()
        cred = BasicCredential(username="user", password="pass")
        strategy = client._resolve_strategy(cred)
        url = client._build_url(
            "postgresql://{username}:{password}@localhost/db",
            strategy,
            cred,
            defaults={"sslmode": "require"},
            username="user",
        )
        assert "sslmode=require" in url

    def test_query_params_from_strategy(self):
        client = ClientWithStrategies()
        cred = OAuthClientCredential(
            client_id="cid",
            client_secret="csec",
            token_url="https://auth.example.com/token",
            access_token="tok",
        )
        strategy = client._resolve_strategy(cred)
        url = client._build_url(
            "snowflake://{username}@account/db",
            strategy,
            cred,
            username="user",
        )
        assert "authenticator=oauth" in url

    def test_strategy_params_override_connection_params(self):
        """If both strategy and connection_params provide 'password', strategy wins."""
        client = ClientWithStrategies()
        cred = BasicCredential(username="user", password="from_strategy")
        strategy = client._resolve_strategy(cred)
        url = client._build_url(
            "postgresql://{username}:{password}@localhost/db",
            strategy,
            cred,
            username="user",
            password="from_connection_params",
        )
        assert "from_strategy" in url


class TestBackwardCompatAlias:
    def test_add_connection_params_delegates_to_add_url_params(self):
        """SQLClient.add_connection_params still works after refactor."""
        from application_sdk.clients.sql import SQLClient

        client = SQLClient()
        result = client.add_connection_params(
            "postgresql://localhost/db", {"ssl": "true"}
        )
        assert result == "postgresql://localhost/db?ssl=true"


class TestInheritanceChain:
    def test_base_sql_client_is_base_client(self):
        from application_sdk.clients.sql import SQLClient

        assert issubclass(SQLClient, Client)

    def test_base_http_client_is_base_client(self):
        from application_sdk.clients.base import HTTPClient

        assert issubclass(HTTPClient, Client)

    def test_async_sql_client_is_base_client(self):
        from application_sdk.clients.sql import AsyncSQLClient

        assert issubclass(AsyncSQLClient, Client)

    def test_azure_client_is_base_client(self):
        from application_sdk.clients.azure.client import AzureClient

        assert issubclass(AzureClient, Client)

    def test_base_http_client_has_resolve_strategy(self):
        from application_sdk.clients.base import HTTPClient

        assert hasattr(HTTPClient, "_resolve_strategy")

    def test_base_http_client_has_build_url(self):
        from application_sdk.clients.base import HTTPClient

        assert hasattr(HTTPClient, "_build_url")
