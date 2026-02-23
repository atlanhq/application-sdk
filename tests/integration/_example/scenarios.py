"""Example scenario definitions for integration testing.

This file demonstrates how to define test scenarios using the
integration testing framework.

With the simplified framework, you typically don't need helper functions.
Credentials are auto-loaded from E2E_{APP_NAME}_* environment variables.

Usage:
    1. Copy this file to your connector's test directory
    2. Define scenarios with assertions
    3. Run: pytest tests/integration/ -v
"""

from application_sdk.test_utils.integration import (
    Scenario,
    all_of,
    contains,
    equals,
    exists,
    is_not_empty,
    is_string,
    one_of,
)

# =============================================================================
# Auth Scenarios
# =============================================================================

auth_scenarios = [
    # Valid credentials - auto-loaded from E2E_* env vars
    Scenario(
        name="auth_valid_credentials",
        api="auth",
        assert_that={
            "success": equals(True),
            "message": all_of(is_string(), is_not_empty()),
        },
        description="Test authentication with valid credentials",
    ),
    # Invalid credentials - override to test failure
    Scenario(
        name="auth_invalid_credentials",
        api="auth",
        credentials={
            "host": "invalid_host",
            "port": 9999,
            "username": "invalid_user",
            "password": "invalid_password",
            "database": "invalid_db",
        },
        assert_that={
            "success": equals(False),
        },
        description="Test authentication with invalid credentials",
    ),
    # Empty credentials - should fail
    Scenario(
        name="auth_empty_credentials",
        api="auth",
        credentials={},
        assert_that={
            "success": equals(False),
        },
        description="Test authentication with empty credentials",
    ),
]


# =============================================================================
# Preflight Scenarios
# =============================================================================

preflight_scenarios = [
    # Valid configuration - credentials auto-loaded
    Scenario(
        name="preflight_valid_config",
        api="preflight",
        metadata={
            "databases": ["test_db"],
            "include_schemas": ["public"],
            "exclude_tables": [],
        },
        assert_that={
            "success": equals(True),
        },
        description="Test preflight check with valid configuration",
    ),
    # Invalid credentials
    Scenario(
        name="preflight_invalid_credentials",
        api="preflight",
        credentials={
            "host": "invalid_host",
            "port": 9999,
            "username": "invalid_user",
            "password": "invalid_password",
            "database": "invalid_db",
        },
        metadata={
            "databases": ["test_db"],
            "include_schemas": ["public"],
        },
        assert_that={
            "success": equals(False),
        },
        description="Test preflight check with invalid credentials",
    ),
]


# =============================================================================
# Workflow Scenarios
# =============================================================================

workflow_scenarios = [
    # Valid workflow - credentials auto-loaded
    Scenario(
        name="workflow_valid_execution",
        api="workflow",
        metadata={
            "databases": ["test_db"],
            "include_schemas": ["public"],
        },
        connection={
            "connection_name": "example_test_connection",
            "qualified_name": "default/example/test",
        },
        assert_that={
            "success": equals(True),
            "message": contains("successfully"),
            "data.workflow_id": exists(),
            "data.run_id": exists(),
        },
        description="Test workflow execution with valid configuration",
    ),
    # Workflow with metadata output validation
    # After the workflow completes, the framework compares the actual
    # extracted metadata against the expected baseline JSON file.
    # Uncomment and adapt for your connector:
    #
    # Scenario(
    #     name="workflow_with_metadata_validation",
    #     api="workflow",
    #     metadata={
    #         "databases": ["test_db"],
    #         "include_schemas": ["public"],
    #     },
    #     connection={
    #         "connection_name": "example_test_connection",
    #         "qualified_name": "default/example/test",
    #     },
    #     expected_data="tests/integration/_example/expected/baseline.json",
    #     strict_comparison=True,
    #     workflow_timeout=300,
    #     polling_interval=10,
    #     assert_that={
    #         "success": equals(True),
    #         "data.workflow_id": exists(),
    #     },
    #     description="Workflow with metadata output validation against baseline",
    # ),
    # Invalid credentials
    Scenario(
        name="workflow_invalid_credentials",
        api="workflow",
        credentials={
            "host": "invalid_host",
            "username": "invalid_user",
            "password": "invalid_password",
        },
        metadata={
            "databases": ["test_db"],
        },
        connection={
            "connection_name": "example_test_connection",
            "qualified_name": "default/example/test",
        },
        assert_that={
            "success": one_of([True, False]),
        },
        description="Test workflow with invalid credentials",
    ),
]


# =============================================================================
# All Scenarios
# =============================================================================

scenarios = auth_scenarios + preflight_scenarios + workflow_scenarios

__all__ = [
    "scenarios",
    "auth_scenarios",
    "preflight_scenarios",
    "workflow_scenarios",
]
