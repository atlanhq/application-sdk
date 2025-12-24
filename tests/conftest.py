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
    """Automatically mock DaprClient for all tests to prevent Dapr health check timeouts."""
    with patch(
        "application_sdk.services.eventstore.DaprClient",
        autospec=True,
    ) as mock_dapr:
        # Create a mock instance that can be used as an async context manager
        mock_instance = AsyncMock()
        mock_dapr.return_value.__aenter__.return_value = mock_instance
        mock_dapr.return_value.__aexit__.return_value = None

        # Mock the async methods to avoid actual Dapr calls
        mock_instance.publish_event = AsyncMock()
        mock_instance.invoke_binding = AsyncMock()

        yield mock_dapr
