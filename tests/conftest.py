"""Global test configuration and fixtures."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def mock_secret_store():
    """Automatically mock SecretStoreInput.get_deployment_secret for all tests."""
    with patch(
        "application_sdk.inputs.secretstore.SecretStoreInput.get_deployment_secret",
        return_value={},
    ):
        yield
