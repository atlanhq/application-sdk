"""Integration testing framework for Apps-SDK.

This module provides a declarative, data-driven approach to integration testing.
Developers define test scenarios as data, and the framework handles everything:
credential loading, server discovery, test execution, and assertion validation.

Quick Start (zero boilerplate):

    1. Set environment variables in .env:
        ATLAN_APPLICATION_NAME=postgres
        E2E_POSTGRES_USERNAME=user
        E2E_POSTGRES_PASSWORD=pass
        E2E_POSTGRES_HOST=localhost
        E2E_POSTGRES_PORT=5432

    2. Define scenarios and a test class:

        >>> from application_sdk.test_utils.integration import (
        ...     Scenario, BaseIntegrationTest, equals, exists, is_true, is_dict
        ... )
        >>>
        >>> class TestMyConnector(BaseIntegrationTest):
        ...     scenarios = [
        ...         Scenario(
        ...             name="auth_works",
        ...             api="auth",
        ...             assert_that={"success": equals(True)},
        ...         ),
        ...         Scenario(
        ...             name="auth_fails",
        ...             api="auth",
        ...             credentials={"username": "bad", "password": "wrong"},
        ...             assert_that={"success": equals(False)},
        ...         ),
        ...         Scenario(
        ...             name="preflight_works",
        ...             api="preflight",
        ...             metadata={"include-filter": '{"^mydb$": ["^public$"]}'},
        ...             assert_that={"success": equals(True), "data": is_dict()},
        ...         ),
        ...     ]

    3. Run: pytest tests/integration/ -v

    That's it! Credentials are auto-loaded from E2E_* env vars.
    Server URL is auto-discovered from ATLAN_APP_HTTP_HOST/PORT.
    Each scenario becomes its own pytest test.

Supported APIs:
- auth: Test authentication (/workflows/v1/auth)
- preflight: Preflight checks (/workflows/v1/check)
- workflow: Start workflow (/workflows/v1/{endpoint})

For detailed documentation, see:
    docs/docs/guides/integration-testing.md
"""

# =============================================================================
# Models
# =============================================================================

from .models import APIType, Scenario, ScenarioResult

# =============================================================================
# Lazy Evaluation
# =============================================================================

from .lazy import Lazy, evaluate_if_lazy, is_lazy, lazy

# =============================================================================
# Assertion DSL
# =============================================================================

from .assertions import (
    # Basic assertions
    equals,
    not_equals,
    exists,
    is_none,
    is_true,
    is_false,
    # Collection assertions
    one_of,
    not_one_of,
    contains,
    not_contains,
    has_length,
    is_empty,
    is_not_empty,
    # Numeric assertions
    greater_than,
    greater_than_or_equal,
    less_than,
    less_than_or_equal,
    between,
    # String assertions
    matches,
    starts_with,
    ends_with,
    # Type assertions
    is_type,
    is_dict,
    is_list,
    is_string,
    # Combinators
    all_of,
    any_of,
    none_of,
    # Custom
    custom,
)

# =============================================================================
# Client
# =============================================================================

from .client import IntegrationTestClient

# =============================================================================
# Test Runner
# =============================================================================

from .runner import BaseIntegrationTest, generate_test_methods, parametrize_scenarios

# =============================================================================
# Public API
# =============================================================================

__all__ = [
    # Models
    "APIType",
    "Scenario",
    "ScenarioResult",
    # Lazy evaluation
    "Lazy",
    "lazy",
    "is_lazy",
    "evaluate_if_lazy",
    # Assertions - Basic
    "equals",
    "not_equals",
    "exists",
    "is_none",
    "is_true",
    "is_false",
    # Assertions - Collections
    "one_of",
    "not_one_of",
    "contains",
    "not_contains",
    "has_length",
    "is_empty",
    "is_not_empty",
    # Assertions - Numeric
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "between",
    # Assertions - String
    "matches",
    "starts_with",
    "ends_with",
    # Assertions - Type
    "is_type",
    "is_dict",
    "is_list",
    "is_string",
    # Assertions - Combinators
    "all_of",
    "any_of",
    "none_of",
    "custom",
    # Client
    "IntegrationTestClient",
    # Runner
    "BaseIntegrationTest",
    "generate_test_methods",
    "parametrize_scenarios",
]
