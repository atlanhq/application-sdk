"""
Azure authentication provider for the application-sdk framework.

This module provides the AzureAuthProvider class that handles Azure
Service Principal authentication for the application-sdk framework.

Example:
    >>> from application_sdk.clients.azure.auth import AzureAuthProvider
    >>> import asyncio
    >>>
    >>> # Create authentication provider
    >>> auth_provider = AzureAuthProvider()
    >>>
    >>> # Authenticate with Service Principal credentials
    >>> credentials = {
    ...     "tenant_id": "your-tenant-id",
    ...     "client_id": "your-client-id",
    ...     "client_secret": "your-client-secret"
    ... }
    >>>
    >>> # Create credential
    >>> credential = await auth_provider.create_credential(
    ...     auth_type="service_principal",
    ...     credentials=credentials
    ... )
    >>>
    >>> # Alternative credential key formats are also supported
    >>> alt_credentials = {
    ...     "tenantId": "your-tenant-id",      # camelCase
    ...     "clientId": "your-client-id",      # camelCase
    ...     "clientSecret": "your-client-secret"  # camelCase
    ... }
    >>>
    >>> credential = await auth_provider.create_credential(
    ...     auth_type="service_principal",
    ...     credentials=alt_credentials
    ... )
    >>>
    >>> # Error handling for missing credentials
    >>> try:
    ...     await auth_provider.create_credential(
    ...         auth_type="service_principal",
    ...         credentials={"tenant_id": "only-tenant"}  # Missing client_id and client_secret
    ...     )
    ... except CommonError as e:
    ...     print(f"Authentication failed: {e}")
    ...     # Output: Authentication failed: Missing required credential keys: client_id, client_secret
    >>>
    >>> # Unsupported authentication type
    >>> try:
    ...     await auth_provider.create_credential(
    ...         auth_type="unsupported_type",
    ...         credentials=credentials
    ...     )
    ... except CommonError as e:
    ...     print(f"Authentication failed: {e}")
    ...     # Output: Authentication failed: Only 'service_principal' authentication is supported. Received: unsupported_type
"""

from typing import Any, Dict, Optional

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import ClientSecretCredential
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from application_sdk.clients.azure import AZURE_MANAGEMENT_API_ENDPOINT
from application_sdk.common.error_codes import CommonError
from application_sdk.common.utils import run_sync
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class ServicePrincipalCredentials(BaseModel):
    """
    Pydantic model for Azure Service Principal credentials.

    Supports both snake_case and camelCase field names through field aliases.
    All fields are required for service principal authentication.

    Attributes:
        tenant_id: Azure tenant ID (also accepts 'tenantId').
        client_id: Azure client ID (also accepts 'clientId').
        client_secret: Azure client secret (also accepts 'clientSecret').
    """

    tenant_id: str = Field(
        ...,
        alias="tenantId",
        description="Azure tenant ID for service principal authentication",
    )
    client_id: str = Field(
        ...,
        alias="clientId",
        description="Azure client ID for service principal authentication",
    )
    client_secret: str = Field(
        ...,
        alias="clientSecret",
        description="Azure client secret for service principal authentication",
    )

    model_config = ConfigDict(
        populate_by_name=True,  # Allow both field name and alias
        extra="ignore",  # Ignore additional fields (Azure client may need extra fields like storage_account_name, network_config, etc.)
        validate_assignment=True,  # Validate on assignment
    )


class AzureAuthProvider:
    """
    Azure authentication provider for handling Service Principal authentication.

    This class provides a unified interface for creating Azure credentials
    using Service Principal authentication with Azure SDK.

    Supported authentication method:
    - service_principal: Using client ID, client secret, and tenant ID
    """

    def __init__(self):
        """Initialize the Azure authentication provider."""
        pass

    async def create_credential(
        self,
        auth_type: str = "service_principal",
        credentials: Optional[Dict[str, Any]] = None,
    ) -> TokenCredential:
        """
        Create Azure credential using Service Principal authentication.

        Args:
            auth_type (str): Type of authentication to use.
                Currently only supports 'service_principal'.
            credentials (Optional[Dict[str, Any]]): Service Principal credentials.
                Required fields: tenant_id, client_id, client_secret.

        Returns:
            TokenCredential: Azure credential instance.

        Raises:
            CommonError: If authentication type is not supported or credentials are invalid.
            ClientAuthenticationError: If credential creation fails.
        """
        try:
            logger.debug(f"Creating Azure credential with auth type: {auth_type}")

            if auth_type.lower() != "service_principal":
                raise CommonError(
                    f"{CommonError.CREDENTIALS_PARSE_ERROR}: "
                    f"Only 'service_principal' authentication is supported. "
                    f"Received: {auth_type}"
                )

            if not credentials:
                raise CommonError(
                    f"{CommonError.CREDENTIALS_PARSE_ERROR}: "
                    "Credentials required for service principal authentication"
                )

            return await self._create_service_principal_credential(credentials)

        except ClientAuthenticationError as e:
            logger.error(f"Azure authentication failed: {str(e)}")
            raise CommonError(f"{CommonError.AZURE_CREDENTIAL_ERROR}: {str(e)}")
        except ValueError as e:
            logger.error(f"Invalid Azure credential parameters: {str(e)}")
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: Invalid credential parameters - {str(e)}"
            )
        except TypeError as e:
            logger.error(f"Wrong Azure credential parameter types: {str(e)}")
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: Invalid credential parameter types - {str(e)}"
            )
        except Exception as e:
            logger.error(f"Unexpected error creating Azure credential: {str(e)}")
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: Unexpected error - {str(e)}"
            )

    async def _create_service_principal_credential(
        self, credentials: Dict[str, Any]
    ) -> ClientSecretCredential:
        """
        Create service principal credential.

        Args:
            credentials (Dict[str, Any]): Service principal credentials.
                Must include tenant_id, client_id, and client_secret.

        Returns:
            ClientSecretCredential: Service principal credential.

        Raises:
            CommonError: If required credentials are missing or invalid.
        """
        if not credentials:
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: "
                "Credentials required for service principal authentication"
            )

        try:
            # Validate credentials using Pydantic model
            validated_credentials = ServicePrincipalCredentials(**credentials)
        except ValidationError as e:
            # Pydantic provides detailed error messages for all validation errors
            # Format errors into a user-friendly message
            error_details = "; ".join(
                [
                    f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                    for err in e.errors()
                ]
            )
            error_message = f"Invalid credential parameters: {error_details}"
            logger.error(f"Azure credential validation failed: {error_message}")
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: {error_message}"
            )

        logger.debug(
            f"Creating service principal credential for tenant: {validated_credentials.tenant_id}"
        )

        try:
            return await run_sync(ClientSecretCredential)(
                validated_credentials.tenant_id,
                validated_credentials.client_id,
                validated_credentials.client_secret,
            )
        except ValueError as e:
            logger.error(f"Invalid Azure credential parameters: {str(e)}")
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: Invalid credential parameters - {str(e)}"
            )
        except TypeError as e:
            logger.error(f"Wrong Azure credential parameter types: {str(e)}")
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: Invalid credential parameter types - {str(e)}"
            )
        except ClientAuthenticationError as e:
            logger.error(f"Azure authentication failed: {str(e)}")
            raise CommonError(f"{CommonError.AZURE_CREDENTIAL_ERROR}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error creating Azure credential: {str(e)}")
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: Unexpected error - {str(e)}"
            )

    async def validate_credential(self, credential: TokenCredential) -> bool:
        """
        Validate Azure credential by attempting to get a token.

        Args:
            credential (TokenCredential): Azure credential to validate.

        Returns:
            bool: True if credential is valid, False otherwise.
        """
        try:
            logger.debug("Validating Azure credential")

            # Try to get a token for Azure Management API
            token = await run_sync(credential.get_token)(
                AZURE_MANAGEMENT_API_ENDPOINT
            )

            if token and hasattr(token, "token"):
                logger.debug("Azure credential validation successful")
                return True
            else:
                logger.warning("Azure credential validation failed: No token received")
                return False

        except ClientAuthenticationError as e:
            logger.error(
                f"Azure credential validation failed - authentication error: {str(e)}"
            )
            return False
        except ValueError as e:
            logger.error(
                f"Azure credential validation failed - invalid parameters: {str(e)}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Azure credential validation failed - unexpected error: {str(e)}"
            )
            return False
