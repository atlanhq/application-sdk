"""Global test configuration and fixtures."""

from unittest.mock import Mock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_secret_store():
    """Automatically mock SecretStore.get_deployment_secret for all tests."""

    def mock_get_deployment_secret(key: str):
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
        "application_sdk.services.eventstore.clients.DaprClient",
        autospec=True,
    ) as mock_dapr:
        # Create a mock instance that can be used as a context manager
        mock_instance = Mock()
        mock_dapr.return_value.__enter__.return_value = mock_instance
        mock_dapr.return_value.__exit__.return_value = None

        # Mock the publish_event method to avoid actual Dapr calls
        mock_instance.publish_event = Mock()
        mock_instance.invoke_binding = Mock()

        yield mock_dapr
