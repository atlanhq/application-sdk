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

import asyncio
from typing import Any, Dict, Optional

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import ClientSecretCredential

from application_sdk.common.error_codes import CommonError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


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

            return await self._create_service_principal_credential(credentials)

        except ClientAuthenticationError:
            raise
        except Exception as e:
            logger.error(f"Failed to create Azure credential: {str(e)}")
            raise CommonError(f"{CommonError.CREDENTIALS_PARSE_ERROR}: {str(e)}")

    async def _create_service_principal_credential(
        self, credentials: Optional[Dict[str, Any]]
    ) -> ClientSecretCredential:
        """
        Create service principal credential.

        Args:
            credentials (Optional[Dict[str, Any]]): Service principal credentials.

        Returns:
            ClientSecretCredential: Service principal credential.

        Raises:
            CommonError: If required credentials are missing.
        """
        if not credentials:
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: "
                "Credentials required for service principal authentication"
            )

        tenant_id = credentials.get("tenant_id") or credentials.get("tenantId")
        client_id = credentials.get("client_id") or credentials.get("clientId")
        client_secret = credentials.get("client_secret") or credentials.get(
            "clientSecret"
        )

        # Check which specific keys are missing and provide detailed error message
        missing_keys = []
        if not tenant_id:
            missing_keys.append("tenant_id")
        if not client_id:
            missing_keys.append("client_id")
        if not client_secret:
            missing_keys.append("client_secret")

        if missing_keys:
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: "
                f"Missing required credential keys: {', '.join(missing_keys)}. "
                "All of tenant_id, client_id, and client_secret are required for "
                "service principal authentication"
            )

        logger.debug(f"Creating service principal credential for tenant: {tenant_id}")

        # Ensure all values are strings
        tenant_id_str = str(tenant_id) if tenant_id else ""
        client_id_str = str(client_id) if client_id else ""
        client_secret_str = str(client_secret) if client_secret else ""

        return await asyncio.get_event_loop().run_in_executor(
            None,
            ClientSecretCredential,
            tenant_id_str,
            client_id_str,
            client_secret_str,
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
            token = await asyncio.get_event_loop().run_in_executor(
                None, credential.get_token, "https://management.azure.com/.default"
            )

            if token and token.token:
                logger.debug("Azure credential validation successful")
                return True
            else:
                logger.warning("Azure credential validation failed: No token received")
                return False

        except Exception as e:
            logger.error(f"Azure credential validation failed: {str(e)}")
            return False
