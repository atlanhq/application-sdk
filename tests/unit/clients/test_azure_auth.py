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
        # Pydantic provides clear error messages about missing fields
        assert "tenant" in error_message.lower() or "tenantId" in error_message
        assert "Field required" in error_message or "required" in error_message.lower()

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
        # Pydantic provides clear error messages about missing fields
        assert "client" in error_message.lower() or "clientId" in error_message
        assert "Field required" in error_message or "required" in error_message.lower()

    @pytest.mark.asyncio
    async def test_missing_client_secret(self, auth_provider):
        """Test error message when client_secret is missing."""
        credentials = {"tenant_id": "test_tenant_id", "client_id": "test_client_id"}

        with pytest.raises(CommonError) as exc_info:
            await auth_provider._create_service_principal_credential(credentials)

        error_message = str(exc_info.value)
        # Pydantic provides clear error messages about missing fields
        assert (
            "client_secret" in error_message.lower() or "clientSecret" in error_message
        )
        assert "Field required" in error_message or "required" in error_message.lower()

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
        # Pydantic provides clear error messages about all missing fields
        assert "client" in error_message.lower()
        assert "Field required" in error_message or "required" in error_message.lower()

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

    @pytest.mark.asyncio
    async def test_extra_fields_ignored(self, auth_provider):
        """Test that extra fields (like storage_account_name, network_config) are ignored by Pydantic validation."""
        credentials = {
            "tenant_id": "test_tenant_id",
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
            "storage_account_name": "test_storage_account",
            "network_config": {"some": "config"},
            "azure_tenant_id": "another_tenant_id",  # This should be ignored, not validated
        }

        # This should not raise an error about extra fields
        try:
            await auth_provider._create_service_principal_credential(credentials)
        except Exception as e:
            # If it fails, it should be due to actual credential validation, not extra fields
            assert "Extra inputs are not permitted" not in str(e)
            assert "Missing required credential keys" not in str(e)
