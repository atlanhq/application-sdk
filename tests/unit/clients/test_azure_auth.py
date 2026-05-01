"""Unit tests for Azure auth module."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from azure.core.exceptions import ClientAuthenticationError

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

    # ------------------------------------------------------------------
    # Tests for create_credential() — error paths and dispatch
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_create_credential_none_credentials_raises(self, auth_provider):
        """create_credential with no credentials must raise CommonError."""
        with pytest.raises(CommonError) as exc_info:
            await auth_provider.create_credential(
                auth_type="service_principal", credentials=None
            )
        assert "Credentials required for service principal authentication" in str(
            exc_info.value
        )

    @pytest.mark.asyncio
    async def test_create_credential_empty_credentials_raises(self, auth_provider):
        """create_credential with empty dict credentials must raise CommonError."""
        with pytest.raises(CommonError) as exc_info:
            await auth_provider.create_credential(
                auth_type="service_principal", credentials={}
            )
        # falsy {} matches the same `if not credentials` branch
        assert "Credentials required for service principal authentication" in str(
            exc_info.value
        )

    @pytest.mark.asyncio
    async def test_create_credential_unsupported_auth_type_default_dict(
        self, auth_provider
    ):
        """Unsupported auth type with non-empty creds reaches the auth_type guard."""
        with pytest.raises(CommonError) as exc_info:
            await auth_provider.create_credential(
                auth_type="oauth",
                credentials={
                    "tenant_id": "t",
                    "client_id": "c",
                    "client_secret": "s",
                },
            )
        assert "Only 'service_principal' authentication is supported" in str(
            exc_info.value
        )

    @pytest.mark.asyncio
    async def test_create_credential_success_dispatches(self, auth_provider):
        """Happy path: dispatches to _create_service_principal_credential."""
        creds = {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
        sentinel = object()
        with patch.object(
            auth_provider,
            "_create_service_principal_credential",
            new=AsyncMock(return_value=sentinel),
        ) as mock_inner:
            result = await auth_provider.create_credential(
                auth_type="service_principal", credentials=creds
            )
        assert result is sentinel
        mock_inner.assert_called_once_with(creds)

    @pytest.mark.asyncio
    async def test_create_credential_wraps_value_error(self, auth_provider):
        """ValueError from inner method becomes CommonError CREDENTIALS_PARSE_ERROR."""
        with patch.object(
            auth_provider,
            "_create_service_principal_credential",
            new=AsyncMock(side_effect=ValueError("bad")),
        ):
            with pytest.raises(CommonError) as exc_info:
                await auth_provider.create_credential(
                    auth_type="service_principal",
                    credentials={
                        "tenant_id": "t",
                        "client_id": "c",
                        "client_secret": "s",
                    },
                )
        assert "Invalid credential parameters" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_credential_wraps_type_error(self, auth_provider):
        """TypeError from inner method becomes CommonError CREDENTIALS_PARSE_ERROR."""
        with patch.object(
            auth_provider,
            "_create_service_principal_credential",
            new=AsyncMock(side_effect=TypeError("bad type")),
        ):
            with pytest.raises(CommonError) as exc_info:
                await auth_provider.create_credential(
                    auth_type="service_principal",
                    credentials={
                        "tenant_id": "t",
                        "client_id": "c",
                        "client_secret": "s",
                    },
                )
        assert "Invalid credential parameter types" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_credential_wraps_client_auth_error(self, auth_provider):
        """ClientAuthenticationError gets mapped to AZURE_CREDENTIAL_ERROR."""
        with patch.object(
            auth_provider,
            "_create_service_principal_credential",
            new=AsyncMock(side_effect=ClientAuthenticationError("auth fail")),
        ):
            with pytest.raises(CommonError) as exc_info:
                await auth_provider.create_credential(
                    auth_type="service_principal",
                    credentials={
                        "tenant_id": "t",
                        "client_id": "c",
                        "client_secret": "s",
                    },
                )
        assert "auth fail" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_credential_wraps_unexpected_error(self, auth_provider):
        """Generic Exception is wrapped as 'Unexpected error'."""
        with patch.object(
            auth_provider,
            "_create_service_principal_credential",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(CommonError) as exc_info:
                await auth_provider.create_credential(
                    auth_type="service_principal",
                    credentials={
                        "tenant_id": "t",
                        "client_id": "c",
                        "client_secret": "s",
                    },
                )
        assert "Unexpected error" in str(exc_info.value)

    # ------------------------------------------------------------------
    # Tests for _create_service_principal_credential — successful path
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_create_service_principal_success(self, auth_provider):
        """run_in_thread is called with snake_case args and result is returned."""
        sentinel = Mock(name="ClientSecretCredential")
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(return_value=sentinel),
        ) as mock_rit:
            result = await auth_provider._create_service_principal_credential(
                {"tenant_id": "t-id", "client_id": "c-id", "client_secret": "s-id"}
            )
        assert result is sentinel
        # Validate run_in_thread was called with positional args from validated model
        args = mock_rit.call_args.args
        assert args[1:] == ("t-id", "c-id", "s-id")

    @pytest.mark.asyncio
    async def test_create_service_principal_run_in_thread_value_error(
        self, auth_provider
    ):
        """ValueError from run_in_thread becomes CommonError."""
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(side_effect=ValueError("bad value")),
        ):
            with pytest.raises(CommonError) as exc_info:
                await auth_provider._create_service_principal_credential(
                    {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
                )
        assert "Invalid credential parameters" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_service_principal_run_in_thread_type_error(
        self, auth_provider
    ):
        """TypeError from run_in_thread becomes CommonError."""
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(side_effect=TypeError("bad type")),
        ):
            with pytest.raises(CommonError) as exc_info:
                await auth_provider._create_service_principal_credential(
                    {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
                )
        assert "Invalid credential parameter types" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_service_principal_run_in_thread_client_auth_error(
        self, auth_provider
    ):
        """ClientAuthenticationError from run_in_thread becomes CommonError."""
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(side_effect=ClientAuthenticationError("nope")),
        ):
            with pytest.raises(CommonError) as exc_info:
                await auth_provider._create_service_principal_credential(
                    {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
                )
        assert "nope" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_service_principal_run_in_thread_unexpected_error(
        self, auth_provider
    ):
        """Generic exception in run_in_thread becomes 'Unexpected error'."""
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(CommonError) as exc_info:
                await auth_provider._create_service_principal_credential(
                    {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
                )
        assert "Unexpected error" in str(exc_info.value)

    # ------------------------------------------------------------------
    # Tests for validate_credential
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_validate_credential_success(self, auth_provider):
        """Token returned with .token attribute → True."""
        token = Mock()
        token.token = "abc"
        credential = Mock()
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(return_value=token),
        ):
            assert await auth_provider.validate_credential(credential) is True

    @pytest.mark.asyncio
    async def test_validate_credential_no_token_returned(self, auth_provider):
        """run_in_thread returns None or token without .token → False."""
        credential = Mock()
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(return_value=None),
        ):
            assert await auth_provider.validate_credential(credential) is False

    @pytest.mark.asyncio
    async def test_validate_credential_token_missing_attr(self, auth_provider):
        """Token object without .token attribute → False."""
        credential = Mock()
        # An object that is truthy but lacks `.token`
        bad_token = type("X", (), {})()
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(return_value=bad_token),
        ):
            assert await auth_provider.validate_credential(credential) is False

    @pytest.mark.asyncio
    async def test_validate_credential_client_auth_error(self, auth_provider):
        """ClientAuthenticationError → False."""
        credential = Mock()
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(side_effect=ClientAuthenticationError("denied")),
        ):
            assert await auth_provider.validate_credential(credential) is False

    @pytest.mark.asyncio
    async def test_validate_credential_value_error(self, auth_provider):
        """ValueError in run_in_thread → False (param error path)."""
        credential = Mock()
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(side_effect=ValueError("bad")),
        ):
            assert await auth_provider.validate_credential(credential) is False

    @pytest.mark.asyncio
    async def test_validate_credential_unexpected_error(self, auth_provider):
        """Generic exception → False (defensive last-resort branch)."""
        credential = Mock()
        with patch(
            "application_sdk.clients.azure.auth.run_in_thread",
            new=AsyncMock(side_effect=RuntimeError("oops")),
        ):
            assert await auth_provider.validate_credential(credential) is False
