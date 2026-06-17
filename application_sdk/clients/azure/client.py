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
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError, ClientAuthenticationError
from pydantic import BaseModel

from application_sdk.clients._interface import ClientInterface
from application_sdk.clients.azure import AZURE_MANAGEMENT_API_ENDPOINT
from application_sdk.clients.azure.auth import AzureAuthProvider
from application_sdk.clients.azure.azure_errors import (
    AzureClientAuthError,
    AzureInputValidationError,
)
from application_sdk.errors import AppError
from application_sdk.execution.heartbeat import run_in_thread
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class ServiceHealth(BaseModel):
    """Model for individual service health status.

    Attributes:
        status: The health status of the service (e.g., "healthy", "error", "unknown")
        error: Optional error message if the service is unhealthy
    """

    status: str
    error: str | None = None


class HealthStatus(BaseModel):
    """Model for overall Azure client health status.

    Attributes:
        connection_health: Whether the Azure connection is healthy
        services: Dictionary mapping service names to their health status
        overall_health: Overall health status considering connection and services
    """

    connection_health: bool
    services: dict[str, ServiceHealth]
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
        credentials: dict[str, Any] | None = None,
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
        self.resolved_credentials: dict[str, Any] = {}
        self.credential: TokenCredential | None = None
        self.auth_provider = AzureAuthProvider()
        self._services: dict[str, Any] = {}
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=max_workers
        )
        self._connection_health = False
        self._kwargs = kwargs

    async def load(self, credentials: dict[str, Any] | None = None) -> None:
        """
        Load and establish Azure connection using Service Principal authentication.

        Args:
            credentials (Optional[Dict[str, Any]]): Azure Service Principal credentials.
                If provided, will override the credentials passed to __init__.
                Must include tenant_id, client_id, and client_secret.

        Raises:
            AzureClientAuthError: If connection fails due to authentication or connection issues
            AzureInputValidationError: If connection fails due to invalid input parameters
        """
        if credentials:
            self.credentials = credentials

        if self._executor is None:
            raise AzureClientAuthError(
                message="Azure client has been closed; instantiate a new AzureClient",
            )

        try:
            logger.info("Loading Azure client...")

            # Handle credential resolution
            if "credential_guid" in self.credentials:
                from application_sdk.infrastructure import (  # noqa: PLC0415 — optional dep: dapr (azure-dapr credential pattern)
                    AsyncDaprClient,
                    DaprCredentialVault,
                )

                dapr_client = AsyncDaprClient()
                try:
                    vault = DaprCredentialVault(dapr_client)
                    self.resolved_credentials = await vault.get_credentials(
                        self.credentials["credential_guid"]
                    )
                finally:
                    await dapr_client.close()
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
            raise AzureClientAuthError(cause=e) from e
        except AzureError as e:
            raise AzureClientAuthError(cause=e) from e
        except ValueError as e:
            raise AzureInputValidationError(
                message="Invalid parameters", cause=e
            ) from e
        except TypeError as e:
            raise AzureInputValidationError(
                message="Invalid parameter types", cause=e
            ) from e
        except AppError:
            raise
        # conformance: ignore[E004] catch-all re-raises as typed AzureClientAuthError; no logging needed since caller receives the typed error
        except Exception as e:
            raise AzureClientAuthError(message="Unexpected error", cause=e) from e

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
                except Exception:
                    logger.warning(
                        "Error closing service client %s", service_name, exc_info=True
                    )

            # Clear service cache
            self._services.clear()

            # Shutdown executor
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None

            # Reset connection health
            self._connection_health = False

            logger.info("Azure client closed successfully")

        except Exception:
            logger.error("Error closing Azure client", exc_info=True)

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
                logger.warning(
                    "Service health check failed for %s",
                    service_name,
                    exc_info=True,
                )
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
            AzureClientAuthError: If connection test fails.
        """
        if not self.credential:
            raise AzureClientAuthError(
                message="No credential available for connection test",
            )

        try:
            # Test the credential by getting a token
            await run_in_thread(
                self.credential.get_token, AZURE_MANAGEMENT_API_ENDPOINT
            )
        except ClientAuthenticationError as e:
            raise AzureClientAuthError(cause=e) from e
        except AzureError as e:
            raise AzureClientAuthError(cause=e) from e
        except ValueError as e:
            raise AzureInputValidationError(cause=e) from e
        except AppError:
            raise
        # conformance: ignore[E004] catch-all re-raises as typed AzureClientAuthError; no logging needed since caller receives the typed error
        except Exception as e:
            raise AzureClientAuthError(message="Unexpected error", cause=e) from e

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
            loop = asyncio.get_running_loop()
            loop.create_task(self.close())
        except RuntimeError:
            # No event loop running, can't schedule async cleanup
            logger.warning(
                "No event loop running, async cleanup not possible", exc_info=True
            )

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
