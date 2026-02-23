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

from .assertions import (  # Basic assertions; Collection assertions; Numeric assertions; String assertions; Type assertions; Combinators; Custom
    all_of,
    any_of,
    between,
    contains,
    custom,
    ends_with,
    equals,
    exists,
    greater_than,
    greater_than_or_equal,
    has_length,
    is_dict,
    is_empty,
    is_false,
    is_list,
    is_none,
    is_not_empty,
    is_string,
    is_true,
    is_type,
    less_than,
    less_than_or_equal,
    matches,
    none_of,
    not_contains,
    not_equals,
    not_one_of,
    one_of,
    starts_with,
)
from .client import IntegrationTestClient
from .comparison import (
    AssetDiff,
    GapReport,
    compare_metadata,
    load_actual_output,
    load_expected_data,
)
from .lazy import Lazy, evaluate_if_lazy, is_lazy, lazy
from .models import APIType, Scenario, ScenarioResult
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
    # Metadata Comparison
    "AssetDiff",
    "GapReport",
    "compare_metadata",
    "load_actual_output",
    "load_expected_data",
    # Client
    "IntegrationTestClient",
    # Runner
    "BaseIntegrationTest",
    "generate_test_methods",
    "parametrize_scenarios",
]
