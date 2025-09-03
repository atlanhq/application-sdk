"""Global test configuration and fixtures."""

from unittest.mock import Mock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_secret_store():
    """Automatically mock SecretStore.get_deployment_secret for all tests."""
    with patch(
        "application_sdk.services.secretstore.SecretStore.get_deployment_secret",
        return_value={},
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
