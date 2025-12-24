"""Global test configuration and fixtures."""

from unittest.mock import AsyncMock, Mock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_secret_store():
    """Automatically mock SecretStore.get_deployment_secret for all tests."""

    async def mock_get_deployment_secret(key: str):
        """Default mock that returns None for all keys."""
        return None

    with patch(
        "application_sdk.services.secretstore.SecretStore.get_deployment_secret",
        side_effect=mock_get_deployment_secret,
    ):
        yield


@pytest.fixture(autouse=True)
def mock_dapr_client():
    """Automatically mock all DaprClient usages for all tests to prevent Dapr health check timeouts."""
    mock_async_client = AsyncMock()
    mock_sync_client = Mock()
    mock_sync_client.get_metadata.return_value = Mock(registered_components=[])
    
    with patch(
        "application_sdk.services.eventstore.is_component_registered", return_value=False
    ), patch(
        "application_sdk.services.eventstore.DaprClient"
    ) as mock_evt_dapr, patch(
        "application_sdk.services.objectstore.DaprClient"
    ) as mock_obj_dapr, patch(
        "application_sdk.services.atlan_storage.DaprClient"
    ) as mock_atlan_dapr, patch(
        "application_sdk.services.secretstore.DaprClient"
    ) as mock_sec_dapr, patch(
        "application_sdk.observability.observability.DaprClient"
    ) as mock_obs_dapr, patch(
        "application_sdk.common.dapr_utils.clients.DaprClient"
    ) as mock_utils_dapr:
        # Configure async DaprClients
        for mock_dapr in [mock_evt_dapr, mock_obj_dapr, mock_atlan_dapr, mock_sec_dapr, mock_obs_dapr]:
            mock_dapr.return_value.__aenter__.return_value = mock_async_client
            mock_dapr.return_value.__aexit__.return_value = None
        
        # Configure sync DaprClient for dapr_utils
        mock_utils_dapr.return_value.__enter__.return_value = mock_sync_client
        mock_utils_dapr.return_value.__exit__.return_value = None
        
        yield mock_async_client
