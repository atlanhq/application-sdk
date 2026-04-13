"""Unit tests for Azure Service Principal auth strategy and credential."""

import pytest

from application_sdk.clients.auth_strategies.service_principal import (
    ServicePrincipalAuthStrategy,
)
from application_sdk.credentials.types import ServicePrincipalCredential


class TestServicePrincipalCredential:
    """Test cases for ServicePrincipalCredential."""

    def test_credential_type(self):
        cred = ServicePrincipalCredential(
            tenant_id="t", client_id="c", client_secret="s"
        )
        assert cred.credential_type == "service_principal"

    @pytest.mark.asyncio
    async def test_validate_empty_tenant_id(self):
        from application_sdk.credentials.errors import CredentialValidationError

        cred = ServicePrincipalCredential(
            tenant_id="", client_id="c", client_secret="s"
        )
        with pytest.raises(CredentialValidationError, match="tenant_id"):
            await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_empty_client_id(self):
        from application_sdk.credentials.errors import CredentialValidationError

        cred = ServicePrincipalCredential(
            tenant_id="t", client_id="", client_secret="s"
        )
        with pytest.raises(CredentialValidationError, match="client_id"):
            await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_empty_client_secret(self):
        from application_sdk.credentials.errors import CredentialValidationError

        cred = ServicePrincipalCredential(
            tenant_id="t", client_id="c", client_secret=""
        )
        with pytest.raises(CredentialValidationError, match="client_secret"):
            await cred.validate()

    @pytest.mark.asyncio
    async def test_validate_valid_credential(self):
        cred = ServicePrincipalCredential(
            tenant_id="t", client_id="c", client_secret="s"
        )
        await cred.validate()  # should not raise


class TestServicePrincipalCredentialParsing:
    """Test that the credential parser handles both snake_case and camelCase."""

    def test_parse_snake_case(self):
        from application_sdk.credentials.types import _parse_service_principal

        cred = _parse_service_principal(
            {
                "tenant_id": "t1",
                "client_id": "c1",
                "client_secret": "s1",
            }
        )
        assert cred.tenant_id == "t1"
        assert cred.client_id == "c1"
        assert cred.client_secret == "s1"

    def test_parse_camel_case(self):
        from application_sdk.credentials.types import _parse_service_principal

        cred = _parse_service_principal(
            {
                "tenantId": "t2",
                "clientId": "c2",
                "clientSecret": "s2",
            }
        )
        assert cred.tenant_id == "t2"
        assert cred.client_id == "c2"
        assert cred.client_secret == "s2"

    def test_parse_mixed(self):
        from application_sdk.credentials.types import _parse_service_principal

        cred = _parse_service_principal(
            {
                "tenant_id": "t3",
                "clientId": "c3",
                "client_secret": "s3",
            }
        )
        assert cred.tenant_id == "t3"
        assert cred.client_id == "c3"
        assert cred.client_secret == "s3"


class TestServicePrincipalAuthStrategy:
    """Test cases for ServicePrincipalAuthStrategy."""

    def test_credential_type(self):
        strategy = ServicePrincipalAuthStrategy()
        assert strategy.credential_type is ServicePrincipalCredential

    def test_build_url_params_empty(self):
        strategy = ServicePrincipalAuthStrategy()
        cred = ServicePrincipalCredential(
            tenant_id="t", client_id="c", client_secret="s"
        )
        assert strategy.build_url_params(cred) == {}

    def test_build_headers_empty(self):
        strategy = ServicePrincipalAuthStrategy()
        cred = ServicePrincipalCredential(
            tenant_id="t", client_id="c", client_secret="s"
        )
        assert strategy.build_headers(cred) == {}

    def test_build_connect_args_returns_azure_credential(self):
        strategy = ServicePrincipalAuthStrategy()
        cred = ServicePrincipalCredential(
            tenant_id="test-tenant",
            client_id="test-client",
            client_secret="test-secret",
        )
        connect_args = strategy.build_connect_args(cred)
        assert "azure_credential" in connect_args

        from azure.identity import ClientSecretCredential

        assert isinstance(connect_args["azure_credential"], ClientSecretCredential)


class TestServicePrincipalRegistry:
    """Test that service_principal is registered in the credential registry."""

    def test_registered(self):
        from application_sdk.credentials.registry import CredentialTypeRegistry

        registry = CredentialTypeRegistry()
        cls = registry.get_class("service_principal")
        assert cls is ServicePrincipalCredential
