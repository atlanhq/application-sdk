"""Fixtures specific to the example integration tests.

This file contains pytest fixtures that are only used by the
example integration tests. Copy and modify for your connector.
"""

import os
from typing import Dict, Any

import pytest


@pytest.fixture(scope="module")
def example_credentials() -> Dict[str, Any]:
    """Provide example credentials for testing.

    Returns:
        Dict[str, Any]: Example credentials from environment.
    """
    return {
        "host": os.getenv("EXAMPLE_DB_HOST", "localhost"),
        "port": int(os.getenv("EXAMPLE_DB_PORT", "5432")),
        "username": os.getenv("EXAMPLE_DB_USER", "test_user"),
        "password": os.getenv("EXAMPLE_DB_PASSWORD", "test_password"),
        "database": os.getenv("EXAMPLE_DB_NAME", "test_db"),
    }


@pytest.fixture(scope="module")
def example_metadata() -> Dict[str, Any]:
    """Provide example metadata configuration.

    Returns:
        Dict[str, Any]: Example metadata configuration.
    """
    return {
        "databases": [os.getenv("EXAMPLE_DB_NAME", "test_db")],
        "include_schemas": ["public"],
    }


@pytest.fixture(scope="module")
def example_connection() -> Dict[str, Any]:
    """Provide example connection configuration.

    Returns:
        Dict[str, Any]: Example connection configuration.
    """
    return {
        "connection_name": "example_test_connection",
        "qualified_name": "default/example/test",
    }


@pytest.fixture
def skip_if_no_server():
    """Skip test if server is not available.

    Usage:
        def test_something(skip_if_no_server):
            # Test will be skipped if server not available
            ...
    """
    import requests

    server_url = os.getenv("APP_SERVER_URL", "http://localhost:8000")

    try:
        response = requests.get(f"{server_url}/server/health", timeout=5)
        if response.status_code != 200:
            pytest.skip(f"Server not healthy at {server_url}")
    except requests.RequestException:
        pytest.skip(f"Server not available at {server_url}")


@pytest.fixture
def skip_if_no_database():
    """Skip test if database is not available.

    Customize this fixture for your connector's database.

    Usage:
        def test_something(skip_if_no_database):
            # Test will be skipped if database not available
            ...
    """
    # Example: Check if we can connect to the database
    # Customize this for your connector
    host = os.getenv("EXAMPLE_DB_HOST")
    if not host:
        pytest.skip("Database host not configured (EXAMPLE_DB_HOST)")

    # Add actual connection check if needed
    # try:
    #     conn = connect_to_database(...)
    #     conn.close()
    # except Exception:
    #     pytest.skip("Could not connect to database")


# =============================================================================
# README for this file
# =============================================================================
#
# This conftest.py is specific to the _example integration tests.
# When you copy this directory for your connector:
#
# 1. Rename fixtures (example_* -> your_connector_*)
# 2. Update credential loading to match your connector
# 3. Update skip conditions for your external dependencies
# 4. Add any connector-specific fixtures you need
#
# The parent conftest.py (tests/integration/conftest.py) provides
# shared fixtures like load_credentials_from_env() that are available
# to all integration tests.
