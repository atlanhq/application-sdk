"""Shared fixtures and configuration for integration tests.

This module provides pytest fixtures that are available to all
integration tests in this directory.
"""

import os
from typing import Dict, Any

import pytest


@pytest.fixture(scope="session")
def server_host() -> str:
    """Get the application server host from environment.

    Returns:
        str: The server URL (default: http://localhost:8000).
    """
    return os.getenv("APP_SERVER_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def integration_test_config() -> Dict[str, Any]:
    """Get integration test configuration from environment.

    Returns:
        Dict[str, Any]: Configuration dictionary.
    """
    return {
        "server_host": os.getenv("APP_SERVER_URL", "http://localhost:8000"),
        "server_version": os.getenv("APP_SERVER_VERSION", "v1"),
        "workflow_endpoint": os.getenv("WORKFLOW_ENDPOINT", "/start"),
        "timeout": int(os.getenv("INTEGRATION_TEST_TIMEOUT", "30")),
    }


def load_credentials_from_env(prefix: str) -> Dict[str, Any]:
    """Load credentials from environment variables with a given prefix.

    This helper function collects all environment variables that start
    with the given prefix and creates a credentials dictionary.

    Args:
        prefix: The environment variable prefix (e.g., "POSTGRES").

    Returns:
        Dict[str, Any]: Credentials dictionary.

    Example:
        # With environment variables:
        # POSTGRES_HOST=localhost
        # POSTGRES_PORT=5432
        # POSTGRES_USER=test
        
        >>> creds = load_credentials_from_env("POSTGRES")
        >>> creds
        {"host": "localhost", "port": "5432", "user": "test"}
    """
    credentials = {}
    prefix_upper = prefix.upper()

    for key, value in os.environ.items():
        if key.startswith(f"{prefix_upper}_"):
            # Remove prefix and convert to lowercase
            cred_key = key[len(prefix_upper) + 1:].lower()
            credentials[cred_key] = value

    return credentials


@pytest.fixture
def load_creds():
    """Fixture that returns the load_credentials_from_env function.

    This allows test modules to use the credential loader as a fixture.

    Example:
        def test_something(load_creds):
            creds = load_creds("MY_APP")
            assert "username" in creds
    """
    return load_credentials_from_env
