"""Global test configuration and fixtures."""

import os
from unittest.mock import Mock, patch

import pytest

# Disable the Dapr observability sink globally for all unit tests.
# Without this, metrics flushing tries to connect to the Dapr sidecar
# (http://127.0.0.1:3500), which isn't running in unit test environments
# and causes 60-second timeouts per test.
os.environ.setdefault("ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK", "false")


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

    def _make_mock_dapr():
        mock_instance = Mock()
        mock_instance.publish_event = Mock()
        mock_instance.invoke_binding = Mock()
        mock_instance.get_state = Mock(return_value=Mock(data=None))
        mock_instance.save_state = Mock()
        mock_instance.get_secret = Mock(return_value=Mock(secret={}))
        return mock_instance

    def _patch(target):
        mock_dapr = Mock()
        mock_instance = _make_mock_dapr()
        mock_dapr.return_value.__enter__ = Mock(return_value=mock_instance)
        mock_dapr.return_value.__exit__ = Mock(return_value=None)
        return patch(target, mock_dapr)

    with (
        _patch("application_sdk.infrastructure._dapr.client.DaprClient"),
    ):
        yield
