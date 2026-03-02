"""Unit tests for Azure client."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from application_sdk.clients.azure import AZURE_MANAGEMENT_API_ENDPOINT
from application_sdk.clients.azure.client import (
    AzureClient,
    HealthStatus,
    ServiceHealth,
)


class TestServiceHealth:
    """Test cases for ServiceHealth model."""

    def test_service_health_creation(self):
        """Test creating ServiceHealth with required fields."""
        health = ServiceHealth(status="healthy")
        assert health.status == "healthy"
        assert health.error is None

    def test_service_health_with_error(self):
        """Test creating ServiceHealth with error field."""
        health = ServiceHealth(status="error", error="Connection failed")
        assert health.status == "error"
        assert health.error == "Connection failed"

    def test_service_health_defaults(self):
        """Test ServiceHealth default values."""
        health = ServiceHealth(status="unknown")
        assert health.status == "unknown"
        assert health.error is None


class TestHealthStatus:
    """Test cases for HealthStatus model."""

    def test_health_status_creation(self):
        """Test creating HealthStatus with all fields."""
        services = {
            "blob": ServiceHealth(status="healthy"),
            "datalake": ServiceHealth(status="error", error="Auth failed"),
        }
        health = HealthStatus(
            connection_health=True, services=services, overall_health=True
        )
        assert health.connection_health is True
        assert len(health.services) == 2
        assert health.overall_health is True

    def test_health_status_empty_services(self):
        """Test creating HealthStatus with empty services."""
        health = HealthStatus(
            connection_health=False, services={}, overall_health=False
        )
        assert health.connection_health is False
        assert len(health.services) == 0
        assert health.overall_health is False


class TestAzureClient:
    """Test cases for AzureClient."""

    def test_azure_management_api_endpoint_constant(self):
        """Test that the Azure Management API endpoint constant is correctly defined."""
        assert AZURE_MANAGEMENT_API_ENDPOINT == "https://management.azure.com/.default"
        assert AZURE_MANAGEMENT_API_ENDPOINT.startswith("https://")
        # Parse URL to securely check hostname (avoids substring matching vulnerability)
        from urllib.parse import urlparse

        parsed_url = urlparse(AZURE_MANAGEMENT_API_ENDPOINT)
        assert parsed_url.hostname == "management.azure.com"

    @pytest.fixture
    def azure_client(self):
        """Create an AzureClient instance for testing."""
        return AzureClient()

    def test_azure_client_initialization(self, azure_client):
        """Test AzureClient initialization."""
        assert azure_client.credentials == {}
        assert azure_client._connection_health is False
        assert azure_client._services == {}

    @pytest.mark.asyncio
    async def test_health_check_no_connection(self, azure_client):
        """Test health check when connection is not healthy."""
        azure_client._connection_health = False

        result = await azure_client.health_check()

        assert isinstance(result, HealthStatus)
        assert result.connection_health is False
        assert result.services == {}
        assert result.overall_health is False

    @pytest.mark.asyncio
    async def test_health_check_with_services_dict_return(self, azure_client):
        """Test health check with services that return dictionary health status."""
        azure_client._connection_health = True
        azure_client._services = {"blob": MagicMock(), "datalake": MagicMock()}

        # Mock service with dict health check return
        azure_client._services["blob"].health_check = AsyncMock(
            return_value={"status": "healthy"}
        )
        azure_client._services["datalake"].health_check = AsyncMock(
            return_value={"status": "error", "error": "Auth failed"}
        )

        result = await azure_client.health_check()

        assert isinstance(result, HealthStatus)
        assert result.connection_health is True
        assert len(result.services) == 2

        blob_health = result.services["blob"]
        assert isinstance(blob_health, ServiceHealth)
        assert blob_health.status == "healthy"
        assert blob_health.error is None

        datalake_health = result.services["datalake"]
        assert isinstance(datalake_health, ServiceHealth)
        assert datalake_health.status == "error"
        assert datalake_health.error == "Auth failed"

        assert result.overall_health is True

    @pytest.mark.asyncio
    async def test_health_check_with_services_object_return(self, azure_client):
        """Test health check with services that return object with status attribute."""
        azure_client._connection_health = True

        # Create mock service with object health check return
        mock_service = MagicMock()
        mock_health = MagicMock()
        mock_health.status = "healthy"
        mock_health.error = None
        mock_service.health_check = AsyncMock(return_value=mock_health)

        azure_client._services = {"test_service": mock_service}

        result = await azure_client.health_check()

        assert isinstance(result, HealthStatus)
        assert result.connection_health is True
        assert len(result.services) == 1

        service_health = result.services["test_service"]
        assert isinstance(service_health, ServiceHealth)
        assert service_health.status == "healthy"
        assert service_health.error is None

    @pytest.mark.asyncio
    async def test_health_check_with_services_unexpected_return(self, azure_client):
        """Test health check with services that return unexpected types."""
        azure_client._connection_health = True

        # Create mock service with unexpected return type
        mock_service = MagicMock()
        mock_service.health_check = AsyncMock(return_value="unexpected_string")

        azure_client._services = {"test_service": mock_service}

        result = await azure_client.health_check()

        assert isinstance(result, HealthStatus)
        assert result.connection_health is True
        assert len(result.services) == 1

        service_health = result.services["test_service"]
        assert isinstance(service_health, ServiceHealth)
        assert service_health.status == "unknown"
        assert "Unexpected health check return type" in service_health.error

    @pytest.mark.asyncio
    async def test_health_check_with_services_no_health_check_method(
        self, azure_client
    ):
        """Test health check with services that don't have health_check method."""
        azure_client._connection_health = True

        # Create mock service without health_check method
        mock_service = MagicMock()
        del mock_service.health_check  # Remove the method

        azure_client._services = {"test_service": mock_service}

        result = await azure_client.health_check()

        assert isinstance(result, HealthStatus)
        assert result.connection_health is True
        assert len(result.services) == 1

        service_health = result.services["test_service"]
        assert isinstance(service_health, ServiceHealth)
        assert service_health.status == "unknown"
        assert service_health.error is None

    @pytest.mark.asyncio
    async def test_health_check_with_service_exception(self, azure_client):
        """Test health check when a service raises an exception."""
        azure_client._connection_health = True

        # Create mock service that raises an exception
        mock_service = MagicMock()
        mock_service.health_check = AsyncMock(
            side_effect=Exception("Service unavailable")
        )

        azure_client._services = {"test_service": mock_service}

        result = await azure_client.health_check()

        assert isinstance(result, HealthStatus)
        assert result.connection_health is True
        assert len(result.services) == 1

        service_health = result.services["test_service"]
        assert isinstance(service_health, ServiceHealth)
        assert service_health.status == "error"
        assert service_health.error == "Service unavailable"

    @pytest.mark.asyncio
    async def test_health_check_overall_health_calculation(self, azure_client):
        """Test that overall health is calculated correctly."""
        azure_client._connection_health = True
        azure_client._services = {}  # No services

        result = await azure_client.health_check()

        assert result.connection_health is True
        assert len(result.services) == 0
        # Overall health should be False when no services are available
        assert result.overall_health is False

        # Add a healthy service
        mock_service = MagicMock()
        mock_service.health_check = AsyncMock(return_value={"status": "healthy"})
        azure_client._services = {"test_service": mock_service}

        result = await azure_client.health_check()

        assert result.overall_health is True
