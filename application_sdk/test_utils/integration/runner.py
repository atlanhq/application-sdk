"""Test runner for integration testing framework.

This module provides the BaseIntegrationTest class that executes scenarios
and validates assertions. It integrates with pytest for test discovery
and execution.

Key Features:
- Declarative scenario execution
- Lazy evaluation of arguments
- Assertion validation using DSL predicates
- Nested path access for response validation
- Detailed error reporting

Example:
    >>> from application_sdk.test_utils.integration import (
    ...     BaseIntegrationTest, Scenario, equals, lazy
    ... )
    >>> 
    >>> scenarios = [
    ...     Scenario(
    ...         name="auth_valid",
    ...         api="auth",
    ...         args=lazy(lambda: {"credentials": load_creds()}),
    ...         assert_that={"success": equals(True)}
    ...     )
    ... ]
    >>> 
    >>> class MyConnectorTest(BaseIntegrationTest):
    ...     scenarios = scenarios
    ...     server_host = "http://localhost:8000"
"""

import time
from typing import Any, Dict, List, Optional, Type

import pytest

from application_sdk.observability.logger_adaptor import get_logger

from .client import IntegrationTestClient
from .lazy import Lazy, evaluate_if_lazy
from .models import Scenario, ScenarioResult

logger = get_logger(__name__)


class BaseIntegrationTest:
    """Base class for integration tests.

    This class provides the test runner infrastructure for executing
    declarative scenarios. Subclasses define scenarios as class attributes,
    and the framework handles execution and validation.

    Class Attributes:
        scenarios: List of Scenario objects to execute.
        server_host: Base URL of the application server.
        server_version: API version prefix.
        workflow_endpoint: Default endpoint for workflow API.
        timeout: Request timeout in seconds.

    Hooks:
        setup_test_environment: Called before any tests run.
        cleanup_test_environment: Called after all tests complete.
        before_scenario: Called before each scenario.
        after_scenario: Called after each scenario.

    Example:
        >>> class SnowflakeIntegrationTest(BaseIntegrationTest):
        ...     scenarios = [...]
        ...     server_host = "http://localhost:8000"
        ...     workflow_endpoint = "/extract"
        ...     
        ...     @classmethod
        ...     def setup_test_environment(cls):
        ...         # Create test schema
        ...         pass
    """

    # Scenario definitions - subclasses should override
    scenarios: List[Scenario] = []

    # Server configuration
    server_host: str = "http://localhost:8000"
    server_version: str = "v1"
    workflow_endpoint: str = "/start"
    timeout: int = 30

    # Internal state
    client: IntegrationTestClient
    _results: List[ScenarioResult]

    @classmethod
    def setup_class(cls) -> None:
        """Set up the test class before any tests run.

        This method:
        1. Initializes the API client
        2. Calls the setup_test_environment hook
        3. Initializes the results list
        """
        logger.info(f"Setting up integration test class: {cls.__name__}")

        # Initialize the client
        cls.client = IntegrationTestClient(
            host=cls.server_host,
            version=cls.server_version,
            workflow_endpoint=cls.workflow_endpoint,
            timeout=cls.timeout,
        )

        # Initialize results tracking
        cls._results = []

        # Call user-defined setup hook
        if hasattr(cls, "setup_test_environment"):
            logger.info("Running setup_test_environment hook")
            cls.setup_test_environment()

    @classmethod
    def teardown_class(cls) -> None:
        """Tear down the test class after all tests complete.

        This method:
        1. Calls the cleanup_test_environment hook
        2. Logs a summary of results
        """
        logger.info(f"Tearing down integration test class: {cls.__name__}")

        # Call user-defined cleanup hook
        if hasattr(cls, "cleanup_test_environment"):
            logger.info("Running cleanup_test_environment hook")
            cls.cleanup_test_environment()

        # Log summary
        if cls._results:
            passed = sum(1 for r in cls._results if r.success)
            total = len(cls._results)
            logger.info(f"Integration test summary: {passed}/{total} scenarios passed")

    def test_scenarios(self) -> None:
        """Execute all scenarios defined in the scenarios list.

        This is the main test method that pytest discovers and runs.
        It iterates through all scenarios and executes them using subtests.
        """
        if not self.scenarios:
            pytest.skip("No scenarios defined")

        for scenario in self.scenarios:
            with self.subTest(scenario=scenario.name):
                self._execute_scenario(scenario)

    def subTest(self, **kwargs):
        """Context manager for subtests (compatibility with unittest).

        In pytest, we use pytest.raises or similar patterns.
        This provides a compatible interface.
        """
        # Use a simple context manager that logs the subtest
        return _SubTestContext(kwargs.get("scenario", "unknown"))

    def _execute_scenario(self, scenario: Scenario) -> ScenarioResult:
        """Execute a single scenario and return the result.

        Args:
            scenario: The scenario to execute.

        Returns:
            ScenarioResult: The result of the scenario execution.
        """
        logger.info(f"Executing scenario: {scenario.name}")

        # Check if scenario should be skipped
        if scenario.skip:
            logger.info(f"Skipping scenario: {scenario.name} - {scenario.skip_reason}")
            pytest.skip(scenario.skip_reason or "Scenario marked as skip")

        start_time = time.time()
        result = ScenarioResult(scenario=scenario, success=False)

        try:
            # Call before_scenario hook if defined
            if hasattr(self, "before_scenario"):
                self.before_scenario(scenario)

            # Step 1: Evaluate lazy arguments
            args = self._evaluate_args(scenario.args)
            logger.debug(f"Evaluated args for {scenario.name}")

            # Step 2: Call the API
            endpoint = scenario.endpoint or self.workflow_endpoint
            response = self.client.call_api(
                api=scenario.api,
                args=args,
                endpoint_override=endpoint if scenario.api == "workflow" else None,
            )
            result.response = response
            logger.debug(f"API response for {scenario.name}: {response}")

            # Step 3: Validate assertions
            assertion_results = self._validate_assertions(response, scenario.assert_that)
            result.assertion_results = assertion_results

            # Check if all assertions passed
            all_passed = all(assertion_results.values())
            result.success = all_passed

            if not all_passed:
                failed = [k for k, v in assertion_results.items() if not v]
                error_msg = f"Assertions failed for paths: {failed}"
                logger.error(f"Scenario {scenario.name} failed: {error_msg}")
                raise AssertionError(error_msg)

            logger.info(f"Scenario {scenario.name} passed")

        except Exception as e:
            result.error = e
            result.success = False
            logger.error(f"Scenario {scenario.name} failed with error: {e}")
            raise

        finally:
            # Record duration
            result.duration_ms = (time.time() - start_time) * 1000
            self._results.append(result)

            # Call after_scenario hook if defined
            if hasattr(self, "after_scenario"):
                self.after_scenario(scenario, result)

        return result

    def _evaluate_args(self, args: Any) -> Dict[str, Any]:
        """Evaluate scenario arguments, handling lazy values.

        Args:
            args: The scenario arguments (may be Lazy or dict).

        Returns:
            Dict[str, Any]: The evaluated arguments.
        """
        return evaluate_if_lazy(args)

    def _validate_assertions(
        self,
        response: Dict[str, Any],
        assertions: Dict[str, Any],
    ) -> Dict[str, bool]:
        """Validate all assertions against the response.

        Args:
            response: The API response dictionary.
            assertions: Dictionary mapping paths to predicates.

        Returns:
            Dict[str, bool]: Dictionary mapping paths to pass/fail status.
        """
        results = {}

        for path, predicate in assertions.items():
            actual = self._get_nested_value(response, path)
            try:
                passed = predicate(actual)
                results[path] = passed
                if not passed:
                    logger.debug(
                        f"Assertion failed: {path} - "
                        f"expected {getattr(predicate, '__doc__', 'predicate')}, "
                        f"got {actual!r}"
                    )
            except Exception as e:
                logger.error(f"Assertion error for {path}: {e}")
                results[path] = False

        return results

    def _get_nested_value(self, data: Dict[str, Any], path: str) -> Any:
        """Get a value from a nested dictionary using dot notation.

        Args:
            data: The dictionary to search.
            path: Dot-separated path (e.g., "data.workflow_id").

        Returns:
            Any: The value at the path, or None if not found.

        Example:
            >>> data = {"data": {"workflow_id": "123"}}
            >>> _get_nested_value(data, "data.workflow_id")
            "123"
        """
        if not path:
            return data

        parts = path.split(".")
        current = data

        for part in parts:
            if current is None:
                return None

            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                index = int(part)
                current = current[index] if 0 <= index < len(current) else None
            else:
                return None

        return current


class _SubTestContext:
    """Simple context manager for subtest compatibility."""

    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        logger.debug(f"Starting subtest: {self.name}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            logger.debug(f"Subtest {self.name} failed: {exc_val}")
        else:
            logger.debug(f"Subtest {self.name} passed")
        return False  # Don't suppress exceptions


# =============================================================================
# Pytest Integration
# =============================================================================


def generate_test_methods(test_class: Type[BaseIntegrationTest]) -> None:
    """Generate individual test methods for each scenario.

    This function can be used to generate separate test methods for each
    scenario, which provides better pytest output.

    Args:
        test_class: The test class to add methods to.

    Example:
        >>> class MyTest(BaseIntegrationTest):
        ...     scenarios = [...]
        >>> 
        >>> generate_test_methods(MyTest)
        >>> # Now MyTest has test_auth_valid, test_preflight_valid, etc.
    """
    for scenario in test_class.scenarios:
        method_name = f"test_{scenario.name}"

        def make_test(s: Scenario):
            def test_method(self):
                self._execute_scenario(s)

            return test_method

        setattr(test_class, method_name, make_test(scenario))


def parametrize_scenarios(scenarios: List[Scenario]):
    """Create a pytest parametrize decorator for scenarios.

    This provides an alternative way to run scenarios using pytest's
    built-in parametrization.

    Args:
        scenarios: List of scenarios to parametrize.

    Returns:
        A pytest.mark.parametrize decorator.

    Example:
        >>> scenarios = [Scenario(...), Scenario(...)]
        >>> 
        >>> class MyTest(BaseIntegrationTest):
        ...     @parametrize_scenarios(scenarios)
        ...     def test_scenario(self, scenario):
        ...         self._execute_scenario(scenario)
    """
    return pytest.mark.parametrize(
        "scenario",
        scenarios,
        ids=[s.name for s in scenarios],
    )
