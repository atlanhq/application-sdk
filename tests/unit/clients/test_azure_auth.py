"""Unit tests for Azure auth module."""

import pytest

from application_sdk.clients.azure.auth import AzureAuthProvider
from application_sdk.common.error_codes import CommonError


class TestAzureAuthProvider:
    """Test cases for AzureAuthProvider."""

    @pytest.fixture
    def auth_provider(self):
        """Create an AzureAuthProvider instance for testing."""
        return AzureAuthProvider()

    @pytest.mark.asyncio
    async def test_unsupported_auth_type(self, auth_provider):
        """Test that unsupported authentication types raise appropriate errors."""
        with pytest.raises(CommonError) as exc_info:
            await auth_provider.create_credential("unsupported_type", {})

        error_message = str(exc_info.value)
        assert "Only 'service_principal' authentication is supported" in error_message
        assert "Received: unsupported_type" in error_message

    @pytest.mark.asyncio
    async def test_missing_tenant_id(self, auth_provider):
        """Test error message when tenant_id is missing."""
        credentials = {
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
        }

        with pytest.raises(CommonError) as exc_info:
            await auth_provider._create_service_principal_credential(credentials)

        error_message = str(exc_info.value)
        assert "Missing required credential keys: tenant_id" in error_message
        assert (
            "All of tenant_id, client_id, and client_secret are required"
            in error_message
        )

    @pytest.mark.asyncio
    async def test_missing_client_id(self, auth_provider):
        """Test error message when client_id is missing."""
        credentials = {
            "tenant_id": "test_tenant_id",
            "client_secret": "test_client_secret",
        }

        with pytest.raises(CommonError) as exc_info:
            await auth_provider._create_service_principal_credential(credentials)

        error_message = str(exc_info.value)
        assert "Missing required credential keys: client_id" in error_message
        assert (
            "All of tenant_id, client_id, and client_secret are required"
            in error_message
        )

    @pytest.mark.asyncio
    async def test_missing_client_secret(self, auth_provider):
        """Test error message when client_secret is missing."""
        credentials = {"tenant_id": "test_tenant_id", "client_id": "test_client_id"}

        with pytest.raises(CommonError) as exc_info:
            await auth_provider._create_service_principal_credential(credentials)

        error_message = str(exc_info.value)
        assert "Missing required credential keys: client_secret" in error_message
        assert (
            "All of tenant_id, client_id, and client_secret are required"
            in error_message
        )

    @pytest.mark.asyncio
    async def test_missing_multiple_keys(self, auth_provider):
        """Test error message when multiple keys are missing."""
        credentials = {
            "tenant_id": "test_tenant_id"
            # Missing client_id and client_secret
        }

        with pytest.raises(CommonError) as exc_info:
            await auth_provider._create_service_principal_credential(credentials)

        error_message = str(exc_info.value)
        assert (
            "Missing required credential keys: client_id, client_secret"
            in error_message
        )
        assert (
            "All of tenant_id, client_id, and client_secret are required"
            in error_message
        )

    @pytest.mark.asyncio
    async def test_missing_all_keys(self, auth_provider):
        """Test error message when all keys are missing."""
        credentials = {}

        with pytest.raises(CommonError) as exc_info:
            await auth_provider._create_service_principal_credential(credentials)

        error_message = str(exc_info.value)
        # Empty credentials are caught by the "if not credentials" check
        assert (
            "Credentials required for service principal authentication" in error_message
        )

    @pytest.mark.asyncio
    async def test_none_credentials(self, auth_provider):
        """Test error message when credentials is None."""
        with pytest.raises(CommonError) as exc_info:
            await auth_provider._create_service_principal_credential(None)

        error_message = str(exc_info.value)
        assert (
            "Credentials required for service principal authentication" in error_message
        )

    @pytest.mark.asyncio
    async def test_empty_credentials(self, auth_provider):
        """Test error message when credentials is empty dict."""
        with pytest.raises(CommonError) as exc_info:
            await auth_provider._create_service_principal_credential({})

        error_message = str(exc_info.value)
        # Empty credentials are caught by the "if not credentials" check
        assert (
            "Credentials required for service principal authentication" in error_message
        )

    @pytest.mark.asyncio
    async def test_alternative_key_names(self, auth_provider):
        """Test that alternative key names (tenantId, clientId, clientSecret) are supported."""
        credentials = {
            "tenantId": "test_tenant_id",
            "clientId": "test_client_id",
            "clientSecret": "test_client_secret",
        }

        # This should not raise an error
        try:
            await auth_provider._create_service_principal_credential(credentials)
        except Exception as e:
            # If it fails, it should be due to actual credential validation, not missing keys
            assert "Missing required credential keys" not in str(e)

    @pytest.mark.asyncio
    async def test_mixed_key_names(self, auth_provider):
        """Test that mixing standard and alternative key names works."""
        credentials = {
            "tenant_id": "test_tenant_id",
            "clientId": "test_client_id",
            "client_secret": "test_client_secret",
        }

        # This should not raise an error
        try:
            await auth_provider._create_service_principal_credential(credentials)
        except Exception as e:
            # If it fails, it should be due to actual credential validation, not missing keys
            assert "Missing required credential keys" not in str(e)
