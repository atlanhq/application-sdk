"""
Azure client implementation for the application-sdk framework.

This module provides the main AzureClient class that serves as a unified interface
for connecting to and interacting with Azure Storage services. It supports Service Principal
authentication and provides service-specific subclients.

Example:
    >>> from application_sdk.clients.azure.client import AzureClient
    >>> from application_sdk.clients.azure.auth import AzureAuthProvider
    >>>
    >>> # Create Azure client with Service Principal credentials
    >>> credentials = {
    ...     "tenant_id": "your-tenant-id",
    ...     "client_id": "your-client-id",
    ...     "client_secret": "your-client-secret"
    ... }
    >>>
    >>> client = AzureClient(credentials)
    >>> await client.load()
    >>>
    >>> # Check client health
    >>> health_status = await client.health_check()
    >>> print(f"Overall health: {health_status.overall_health}")
    >>> print(f"Connection health: {health_status.connection_health}")
    >>>
    >>> # Access health status details
    >>> for service_name, service_health in health_status.services.items():
    ...     print(f"{service_name}: {service_health.status}")
    ...     if service_health.error:
    ...         print(f"  Error: {service_health.error}")
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError, ClientAuthenticationError
from pydantic import BaseModel

from application_sdk.clients import ClientInterface
from application_sdk.clients.azure import AZURE_MANAGEMENT_API_ENDPOINT
from application_sdk.clients.azure.auth import AzureAuthProvider
from application_sdk.common.error_codes import ClientError
from application_sdk.common.utils import run_sync
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class ServiceHealth(BaseModel):
    """Model for individual service health status.

    Attributes:
        status: The health status of the service (e.g., "healthy", "error", "unknown")
        error: Optional error message if the service is unhealthy
    """

    status: str
    error: Optional[str] = None


class HealthStatus(BaseModel):
    """Model for overall Azure client health status.

    Attributes:
        connection_health: Whether the Azure connection is healthy
        services: Dictionary mapping service names to their health status
        overall_health: Overall health status considering connection and services
    """

    connection_health: bool
    services: Dict[str, ServiceHealth]
    overall_health: bool


class AzureClient(ClientInterface):
    """
    Main Azure client for the application-sdk framework.

    This client provides a unified interface for connecting to and interacting
    with Azure services. It supports Service Principal authentication
    and provides service-specific subclients for different Azure services.

    Attributes:
        credentials (Dict[str, Any]): Azure connection credentials
        resolved_credentials (Dict[str, Any]): Resolved credentials after processing
        credential (DefaultAzureCredential): Azure credential instance
        auth_provider (AzureAuthProvider): Authentication provider instance
        _services (Dict[str, Any]): Cache of service clients
        _executor (ThreadPoolExecutor): Thread pool for async operations
        _connection_health (bool): Connection health status
    """

    def __init__(
        self,
        credentials: Optional[Dict[str, Any]] = None,
        max_workers: int = 10,
        **kwargs: Any,
    ):
        """
        Initialize the Azure client.

        Args:
            credentials (Optional[Dict[str, Any]]): Azure Service Principal credentials.
                Must include tenant_id, client_id, and client_secret.
            max_workers (int): Maximum number of worker threads for async operations.
            **kwargs: Additional keyword arguments passed to service clients.
        """
        self.credentials = credentials or {}
        self.resolved_credentials: Dict[str, Any] = {}
        self.credential: Optional[TokenCredential] = None
        self.auth_provider = AzureAuthProvider()
        self._services: Dict[str, Any] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._connection_health = False
        self._kwargs = kwargs

    async def load(self, credentials: Optional[Dict[str, Any]] = None) -> None:
        """
        Load and establish Azure connection using Service Principal authentication.

        Args:
            credentials (Optional[Dict[str, Any]]): Azure Service Principal credentials.
                If provided, will override the credentials passed to __init__.
                Must include tenant_id, client_id, and client_secret.

        Raises:
            ClientError: If connection fails due to authentication or connection issues
        """
        if credentials:
            self.credentials = credentials

        try:
            logger.info("Loading Azure client...")

            # Handle credential resolution
            if "credential_guid" in self.credentials:
                # If we have a credential_guid, use the async get_credentials function
                from application_sdk.services.secretstore import SecretStore

                self.resolved_credentials = await SecretStore.get_credentials(
                    self.credentials["credential_guid"]
                )
            else:
                # If credentials are already resolved (direct format), use them as-is
                # Check if credentials appear to need resolution but no credential_guid provided
                if (
                    "secret-path" in self.credentials
                    or "credentialSource" in self.credentials
                ):
                    logger.warning(
                        "Credentials appear to need resolution but no credential_guid provided. Using as-is."
                    )
                # Credentials are already in the correct format
                self.resolved_credentials = self.credentials

            # Create Azure credential using Service Principal authentication
            self.credential = await self.auth_provider.create_credential(
                auth_type="service_principal", credentials=self.resolved_credentials
            )

            # Test the connection
            await self._test_connection()

            self._connection_health = True
            logger.info("Azure client loaded successfully")

        except ClientAuthenticationError as e:
            logger.error(f"Azure authentication failed: {str(e)}")
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")
        except AzureError as e:
            logger.error(f"Azure connection error: {str(e)}")
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")
        except ValueError as e:
            logger.error(f"Invalid Azure client parameters: {str(e)}")
            raise ClientError(
                f"{ClientError.INPUT_VALIDATION_ERROR}: Invalid parameters - {str(e)}"
            )
        except TypeError as e:
            logger.error(f"Wrong Azure client parameter types: {str(e)}")
            raise ClientError(
                f"{ClientError.INPUT_VALIDATION_ERROR}: Invalid parameter types - {str(e)}"
            )
        except Exception as e:
            logger.error(f"Unexpected error loading Azure client: {str(e)}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Unexpected error - {str(e)}"
            )

    async def close(self) -> None:
        """Close Azure connections and clean up resources."""
        try:
            logger.info("Closing Azure client...")

            # Close all service clients
            for service_name, service_client in self._services.items():
                try:
                    if hasattr(service_client, "close"):
                        await service_client.close()
                    elif hasattr(service_client, "disconnect"):
                        await service_client.disconnect()
                except Exception as e:
                    logger.warning(f"Error closing {service_name} client: {str(e)}")

            # Clear service cache
            self._services.clear()

            # Shutdown executor
            self._executor.shutdown(wait=True)

            # Reset connection health
            self._connection_health = False

            logger.info("Azure client closed successfully")

        except Exception as e:
            logger.error(f"Error closing Azure client: {str(e)}")

    async def health_check(self) -> HealthStatus:
        """
        Perform health check on Azure connection and services.

        Returns:
            HealthStatus: Health status information.
        """
        health_status = HealthStatus(
            connection_health=self._connection_health,
            services={},
            overall_health=False,
        )

        if not self._connection_health:
            return health_status

        # Check each service
        for service_name, service_client in self._services.items():
            try:
                if hasattr(service_client, "health_check"):
                    service_health = await service_client.health_check()
                    # Handle different return types from service health checks
                    if isinstance(service_health, dict):
                        status = service_health.get("status", "unknown")
                        error = service_health.get("error")
                    elif hasattr(service_health, "status"):
                        # Handle Pydantic models or objects with status attribute
                        status = getattr(service_health, "status", "unknown")
                        error = getattr(service_health, "error", None)
                    else:
                        # Fallback for unexpected return types
                        status = "unknown"
                        error = f"Unexpected health check return type: {type(service_health)}"
                else:
                    status = "unknown"
                    error = None

                health_status.services[service_name] = ServiceHealth(
                    status=status,
                    error=error,
                )
            except Exception as e:
                health_status.services[service_name] = ServiceHealth(
                    status="error", error=str(e)
                )

        # Overall health is True if connection is healthy and at least one service is available
        health_status.overall_health = (
            self._connection_health and len(health_status.services) > 0
        )

        return health_status

    async def _test_connection(self) -> None:
        """
        Test the Azure connection by attempting to get a token.

        Raises:
            ClientAuthenticationError: If connection test fails.
        """
        if not self.credential:
            raise ClientError(
                f"{ClientError.AUTH_CREDENTIALS_ERROR}: No credential available for connection test"
            )

        try:
            # Test the credential by getting a token
            await run_sync(self.credential.get_token)(AZURE_MANAGEMENT_API_ENDPOINT)
        except ClientAuthenticationError as e:
            logger.error(
                f"Azure connection test failed - authentication error: {str(e)}"
            )
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")
        except AzureError as e:
            logger.error(f"Azure connection test failed - service error: {str(e)}")
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")
        except ValueError as e:
            logger.error(f"Azure connection test failed - invalid parameters: {str(e)}")
            raise ClientError(
                f"{ClientError.INPUT_VALIDATION_ERROR}: Invalid parameters - {str(e)}"
            )
        except Exception as e:
            logger.error(f"Azure connection test failed - unexpected error: {str(e)}")
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Unexpected error - {str(e)}"
            )

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        # Note: This is a synchronous context manager.
        # For proper async cleanup, use the async context manager instead.
        # This method is kept for backward compatibility but doesn't guarantee cleanup.
        logger.warning(
            "Using synchronous context manager. For proper async cleanup, "
            "use 'async with AzureClient() as client:' instead."
        )
        # Schedule cleanup but don't wait for it
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.close())
        except RuntimeError:
            # No event loop running, can't schedule async cleanup
            logger.warning("No event loop running, async cleanup not possible")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
