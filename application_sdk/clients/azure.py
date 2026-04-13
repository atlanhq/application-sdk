"""Azure client for the application-sdk framework.

Provides ``AzureClient`` for connecting to Azure services via Service
Principal authentication, using the ``AUTH_STRATEGIES`` pattern.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError, ClientAuthenticationError
from pydantic import BaseModel

from application_sdk.clients.auth_strategies.service_principal import (
    ServicePrincipalAuthStrategy,
)
from application_sdk.clients.client import Client
from application_sdk.common.error_codes import ClientError
from application_sdk.common.utils import run_sync
from application_sdk.credentials.types import ServicePrincipalCredential
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

AZURE_MANAGEMENT_API_ENDPOINT = "https://management.azure.com/.default"


class ServiceHealth(BaseModel):
    """Model for individual service health status."""

    status: str
    error: Optional[str] = None


class HealthStatus(BaseModel):
    """Model for overall Azure client health status."""

    connection_health: bool
    services: Dict[str, ServiceHealth]
    overall_health: bool


class AzureClient(Client):
    """Azure client using Service Principal authentication.

    Uses ``AUTH_STRATEGIES`` for credential handling. The strategy creates
    an Azure ``ClientSecretCredential`` which is stored on ``self.credential``
    for use by service sub-clients.

    Example::

        client = AzureClient()
        cred = ServicePrincipalCredential(
            tenant_id="...", client_id="...", client_secret="..."
        )
        await client.load_with_credential(cred)
    """

    AUTH_STRATEGIES = {
        ServicePrincipalCredential: ServicePrincipalAuthStrategy(),
    }

    def __init__(
        self,
        credentials: Optional[Dict[str, Any]] = None,
        max_workers: int = 10,
        **kwargs: Any,
    ):
        super().__init__(credentials=credentials)
        self.credential: Optional[TokenCredential] = None
        self._services: Dict[str, Any] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._connection_health = False
        self._kwargs = kwargs

    async def load(self, credentials: Optional[Dict[str, Any]] = None) -> None:
        """Load raw dict credentials (legacy path).

        Resolves credential_guid if present, then creates the Azure
        credential. Prefer ``load_with_credential()`` for new code.
        """
        if credentials:
            self.credentials = credentials

        try:
            logger.info("Loading Azure client...")

            resolved = self.credentials
            if "credential_guid" in self.credentials:
                from application_sdk.services.secretstore import SecretStore

                resolved = await SecretStore.get_credentials(
                    self.credentials["credential_guid"]
                )

            # Parse into typed credential and delegate to strategy path
            cred = ServicePrincipalCredential(
                tenant_id=resolved.get("tenant_id", "") or resolved.get("tenantId", ""),
                client_id=resolved.get("client_id", "") or resolved.get("clientId", ""),
                client_secret=resolved.get("client_secret", "")
                or resolved.get("clientSecret", ""),
            )
            await self.load_with_credential(cred)

        except ClientError:
            raise
        except ClientAuthenticationError as e:
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}") from e
        except AzureError as e:
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}") from e
        except Exception as e:
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Unexpected error - {str(e)}"
            ) from e

    async def load_with_credential(
        self,
        credential: ServicePrincipalCredential,  # type: ignore[override]
        **connection_params: str,
    ) -> None:
        """Load a typed ServicePrincipalCredential.

        Uses the registered ``ServicePrincipalAuthStrategy`` to create
        an Azure ``ClientSecretCredential``, then tests the connection.
        """
        strategy = self._resolve_strategy(credential)
        connect_args = strategy.build_connect_args(credential)
        self.credential = connect_args["azure_credential"]

        await self._test_connection()
        self._connection_health = True
        logger.info("Azure client loaded successfully")

    async def close(self) -> None:
        """Close Azure connections and clean up resources."""
        try:
            logger.info("Closing Azure client...")

            for service_name, service_client in self._services.items():
                try:
                    if hasattr(service_client, "close"):
                        await service_client.close()
                    elif hasattr(service_client, "disconnect"):
                        await service_client.disconnect()
                except Exception:
                    logger.warning(
                        "Error closing service client",
                        service_name=service_name,
                        exc_info=True,
                    )

            self._services.clear()
            self._executor.shutdown(wait=True)
            self._connection_health = False
            logger.info("Azure client closed successfully")

        except Exception:
            logger.error("Error closing Azure client", exc_info=True)

    async def health_check(self) -> HealthStatus:
        """Perform health check on Azure connection and services."""
        health_status = HealthStatus(
            connection_health=self._connection_health,
            services={},
            overall_health=False,
        )

        if not self._connection_health:
            return health_status

        for service_name, service_client in self._services.items():
            try:
                if hasattr(service_client, "health_check"):
                    service_health = await service_client.health_check()
                    if isinstance(service_health, dict):
                        status = service_health.get("status", "unknown")
                        error = service_health.get("error")
                    elif hasattr(service_health, "status"):
                        status = getattr(service_health, "status", "unknown")
                        error = getattr(service_health, "error", None)
                    else:
                        status = "unknown"
                        error = f"Unexpected health check return type: {type(service_health)}"
                else:
                    status = "unknown"
                    error = None

                health_status.services[service_name] = ServiceHealth(
                    status=status, error=error
                )
            except Exception as e:
                health_status.services[service_name] = ServiceHealth(
                    status="error", error=str(e)
                )

        health_status.overall_health = (
            self._connection_health and len(health_status.services) > 0
        )
        return health_status

    async def _test_connection(self) -> None:
        """Test the Azure connection by attempting to get a token."""
        if not self.credential:
            raise ClientError(
                f"{ClientError.AUTH_CREDENTIALS_ERROR}: No credential available for connection test"
            )

        try:
            await run_sync(self.credential.get_token)(AZURE_MANAGEMENT_API_ENDPOINT)
        except ClientAuthenticationError as e:
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}") from e
        except AzureError as e:
            raise ClientError(f"{ClientError.CLIENT_AUTH_ERROR}: {str(e)}") from e
        except Exception as e:
            raise ClientError(
                f"{ClientError.CLIENT_AUTH_ERROR}: Unexpected error - {str(e)}"
            ) from e

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        logger.warning(
            "Using synchronous context manager. For proper async cleanup, "
            "use 'async with AzureClient() as client:' instead."
        )
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.close())
        except RuntimeError:
            logger.warning("No event loop running, async cleanup not possible")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
