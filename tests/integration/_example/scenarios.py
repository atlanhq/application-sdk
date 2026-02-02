"""Example scenario definitions for integration testing.

This file demonstrates how to define test scenarios using the
integration testing framework. Copy this file and modify it
for your connector.

Usage:
    1. Copy this file to your connector's test directory
    2. Update load_credentials() with your credential logic
    3. Modify scenarios to match your connector's behavior
    4. Run: pytest tests/integration/your_connector/ -v
"""

import os
from typing import Any, Dict

from application_sdk.test_utils.integration import (
    Scenario,
    all_of,
    contains,
    equals,
    exists,
    is_not_empty,
    is_string,
    lazy,
    one_of,
)


# =============================================================================
# Credential Loading
# =============================================================================


def load_credentials() -> Dict[str, Any]:
    """Load credentials from environment variables.

    Modify this function to load credentials for your connector.

    Returns:
        Dict[str, Any]: Credentials dictionary.

    Raises:
        EnvironmentError: If required environment variables are not set.
    """
    # Example: PostgreSQL-style credentials
    # Customize this for your connector
    credentials = {
        "host": os.getenv("EXAMPLE_DB_HOST", "localhost"),
        "port": int(os.getenv("EXAMPLE_DB_PORT", "5432")),
        "username": os.getenv("EXAMPLE_DB_USER", "test_user"),
        "password": os.getenv("EXAMPLE_DB_PASSWORD", "test_password"),
        "database": os.getenv("EXAMPLE_DB_NAME", "test_db"),
    }

    return credentials


def get_invalid_credentials() -> Dict[str, Any]:
    """Get deliberately invalid credentials for negative tests.

    Returns:
        Dict[str, Any]: Invalid credentials.
    """
    return {
        "host": "invalid_host",
        "port": 9999,
        "username": "invalid_user",
        "password": "invalid_password",
        "database": "invalid_db",
    }


def get_test_metadata() -> Dict[str, Any]:
    """Get metadata configuration for tests.

    Returns:
        Dict[str, Any]: Metadata configuration.
    """
    return {
        "databases": [os.getenv("EXAMPLE_DB_NAME", "test_db")],
        "include_schemas": ["public"],
        "exclude_tables": [],
    }


def get_test_connection() -> Dict[str, Any]:
    """Get connection configuration for workflow tests.

    Returns:
        Dict[str, Any]: Connection configuration.
    """
    return {
        "connection_name": "example_test_connection",
        "qualified_name": "default/example/test",
    }


# =============================================================================
# Auth Scenarios
# =============================================================================

auth_scenarios = [
    # Valid credentials - should succeed
    Scenario(
        name="auth_valid_credentials",
        api="auth",
        args=lazy(lambda: {"credentials": load_credentials()}),
        assert_that={
            "success": equals(True),
            "message": all_of(is_string(), is_not_empty()),
        },
        description="Test authentication with valid credentials",
    ),
    # Invalid credentials - should fail
    Scenario(
        name="auth_invalid_credentials",
        api="auth",
        args={"credentials": get_invalid_credentials()},
        assert_that={
            "success": equals(False),
        },
        description="Test authentication with invalid credentials",
    ),
    # Empty credentials - should fail
    Scenario(
        name="auth_empty_credentials",
        api="auth",
        args={"credentials": {}},
        assert_that={
            "success": equals(False),
        },
        description="Test authentication with empty credentials",
    ),
    # Missing password - should fail
    Scenario(
        name="auth_missing_password",
        api="auth",
        args=lazy(
            lambda: {
                "credentials": {
                    k: v for k, v in load_credentials().items() if k != "password"
                }
            }
        ),
        assert_that={
            "success": equals(False),
        },
        description="Test authentication with missing password",
    ),
]


# =============================================================================
# Preflight Scenarios
# =============================================================================

preflight_scenarios = [
    # Valid configuration - should succeed
    Scenario(
        name="preflight_valid_config",
        api="preflight",
        args=lazy(
            lambda: {
                "credentials": load_credentials(),
                "metadata": get_test_metadata(),
            }
        ),
        assert_that={
            "success": equals(True),
        },
        description="Test preflight check with valid configuration",
    ),
    # Invalid credentials - should fail preflight
    Scenario(
        name="preflight_invalid_credentials",
        api="preflight",
        args={
            "credentials": get_invalid_credentials(),
            "metadata": get_test_metadata(),
        },
        assert_that={
            "success": equals(False),
        },
        description="Test preflight check with invalid credentials",
    ),
    # Empty metadata - should handle gracefully
    Scenario(
        name="preflight_empty_metadata",
        api="preflight",
        args=lazy(
            lambda: {
                "credentials": load_credentials(),
                "metadata": {},
            }
        ),
        assert_that={
            # Depending on implementation, this may succeed or fail
            # Adjust based on your connector's behavior
            "success": one_of([True, False]),
        },
        description="Test preflight check with empty metadata",
    ),
]


# =============================================================================
# Workflow Scenarios
# =============================================================================

workflow_scenarios = [
    # Valid workflow - should start successfully
    Scenario(
        name="workflow_valid_execution",
        api="workflow",
        args=lazy(
            lambda: {
                "credentials": load_credentials(),
                "metadata": get_test_metadata(),
                "connection": get_test_connection(),
            }
        ),
        assert_that={
            "success": equals(True),
            "message": contains("successfully"),
            "data.workflow_id": exists(),
            "data.run_id": exists(),
        },
        description="Test workflow execution with valid configuration",
    ),
    # Invalid credentials - workflow should fail to start or fail during execution
    Scenario(
        name="workflow_invalid_credentials",
        api="workflow",
        args={
            "credentials": get_invalid_credentials(),
            "metadata": get_test_metadata(),
            "connection": get_test_connection(),
        },
        assert_that={
            # Workflow may start but fail, or fail to start
            # Adjust based on your connector's behavior
            "success": one_of([True, False]),
        },
        description="Test workflow with invalid credentials",
    ),
]


# =============================================================================
# All Scenarios
# =============================================================================

# Combine all scenarios into a single list
# This is what the test class will use
scenarios = auth_scenarios + preflight_scenarios + workflow_scenarios


# =============================================================================
# Scenario Subsets (for selective testing)
# =============================================================================

# Export subsets for running specific types of tests
# Usage: pytest -k "auth" or modify test class to use subset

__all__ = [
    "scenarios",
    "auth_scenarios",
    "preflight_scenarios",
    "workflow_scenarios",
    "load_credentials",
    "get_invalid_credentials",
    "get_test_metadata",
    "get_test_connection",
]
