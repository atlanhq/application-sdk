"""
Azure client implementation for the application-sdk framework.

This module provides the main AzureClient class that serves as a unified interface
for connecting to and interacting with Azure Storage services. It supports Service Principal
authentication and provides service-specific subclients.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError, ClientAuthenticationError

from application_sdk.clients import ClientInterface
from application_sdk.clients.azure.azure_auth import AzureAuthProvider
from application_sdk.common.credential_utils import resolve_credentials
from application_sdk.common.error_codes import ClientError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


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

            # Resolve credentials using framework's credential resolution
            self.resolved_credentials = await resolve_credentials(self.credentials)

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
        except Exception as e:
            logger.error(f"Unexpected error loading Azure client: {str(e)}")
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}")

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

    async def get_service_client(self, service_type: str) -> Any:
        """
        Get a service client by type.

        Args:
            service_type (str): Type of service client to retrieve.
                Currently no services are supported.

        Returns:
            Any: The requested service client.

        Raises:
            ValueError: If service_type is not supported.
            ClientError: If client creation fails.
        """
        raise ValueError(f"Unsupported service type: {service_type}")

    async def health_check(self) -> Dict[str, Any]:
        """
        Perform health check on Azure connection and services.

        Returns:
            Dict[str, Any]: Health status information.
        """
        health_status = {
            "connection_health": self._connection_health,
            "services": {},
            "overall_health": False,
        }

        if not self._connection_health:
            return health_status

        # Check each service
        for service_name, service_client in self._services.items():
            try:
                if hasattr(service_client, "health_check"):
                    service_health = await service_client.health_check()
                else:
                    service_health = {"status": "unknown"}

                health_status["services"][service_name] = service_health
            except Exception as e:
                health_status["services"][service_name] = {
                    "status": "error",
                    "error": str(e),
                }

        # Overall health is True if connection is healthy and at least one service is available
        health_status["overall_health"] = (
            self._connection_health and len(health_status["services"]) > 0
        )

        return health_status

    async def _test_connection(self) -> None:
        """
        Test the Azure connection by attempting to get a token.

        Raises:
            ClientAuthenticationError: If connection test fails.
        """
        if not self.credential:
            raise ClientAuthenticationError(
                "No credential available for connection test"
            )

        try:
            # Test the credential by getting a token
            await asyncio.get_event_loop().run_in_executor(
                self._executor,
                self.credential.get_token,
                "https://management.azure.com/.default",
            )
        except Exception as e:
            raise ClientAuthenticationError(f"Connection test failed: {str(e)}")

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
