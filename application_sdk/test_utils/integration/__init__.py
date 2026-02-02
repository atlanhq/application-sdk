"""Integration testing framework for Apps-SDK.

This module provides a declarative, data-driven approach to integration testing.
Developers define test scenarios as data, and the framework handles execution,
assertion, and reporting.

Core Concepts:
- Scenario: A single test case with inputs and expected outputs
- Lazy Evaluation: Defer computation until test execution
- Assertion DSL: Higher-order functions for declarative assertions
- BaseIntegrationTest: The test runner that executes scenarios

Quick Start:
    >>> from application_sdk.test_utils.integration import (
    ...     Scenario, BaseIntegrationTest, lazy, equals, exists
    ... )
    >>> 
    >>> # Define scenarios
    >>> scenarios = [
    ...     Scenario(
    ...         name="auth_valid",
    ...         api="auth",
    ...         args=lazy(lambda: {"credentials": {"user": "test"}}),
    ...         assert_that={"success": equals(True)}
    ...     ),
    ... ]
    >>> 
    >>> # Create test class
    >>> class MyConnectorTest(BaseIntegrationTest):
    ...     scenarios = scenarios
    ...     server_host = "http://localhost:8000"

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
