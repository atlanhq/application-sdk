"""Test runner for integration testing framework.

This module provides the BaseIntegrationTest class that executes scenarios
and validates assertions. It integrates with pytest for test discovery
and execution.

Key Features:
- Auto-discovers credentials from E2E_* environment variables
- Auto-generates individual pytest test methods per scenario
- Server health check before tests run
- Rich assertion error messages showing actual vs expected
- Declarative scenario execution with no boilerplate

Example (simplified - no helper functions needed):
    >>> from application_sdk.test_utils.integration import (
    ...     BaseIntegrationTest, Scenario, equals
    ... )
    >>>
    >>> scenarios = [
    ...     Scenario(
    ...         name="auth_valid",
    ...         api="auth",
    ...         assert_that={"success": equals(True)}
    ...     )
    ... ]
    >>>
    >>> class MyConnectorTest(BaseIntegrationTest):
    ...     scenarios = scenarios
    ...     # Credentials auto-loaded from E2E_{APP_NAME}_* env vars
"""

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type

import orjson
import pytest
import requests as http_requests

from application_sdk.observability.logger_adaptor import get_logger

from .client import IntegrationTestClient
from .comparison import compare_metadata, load_actual_output, load_expected_data
from .lazy import evaluate_if_lazy
from .models import APIType, Scenario, ScenarioResult

logger = get_logger(__name__)

# Sentinel value to distinguish "not provided" from None
_NOT_PROVIDED = object()


def _auto_discover_credentials(scenario_name: str = "") -> Dict[str, Any]:
    """Auto-discover credentials from E2E_* environment variables.

    Checks for scenario-specific env vars first, then falls back to
    app-level defaults.

    Resolution order:
      1. E2E_{SCENARIO_NAME}_* (scenario-specific)
      2. E2E_{APP_NAME}_*      (app-level default)

    For example, with ATLAN_APPLICATION_NAME=postgres and
    scenario_name="preflight_missing_permissions":

      Scenario-specific (checked first):
        E2E_PREFLIGHT_MISSING_PERMISSIONS_USERNAME=restricted_user

      App-level fallback:
        E2E_POSTGRES_USERNAME=admin
        E2E_POSTGRES_PASSWORD=secret
        E2E_POSTGRES_PORT=5432

      Result: {"username": "restricted_user", "password": "secret", "port": 5432}

    Args:
        scenario_name: The scenario name used to look up scenario-specific
            env vars. If empty, only app-level defaults are returned.

    Returns:
        Dict[str, Any]: Auto-discovered credentials from env vars.
    """
    app_name = os.getenv("ATLAN_APPLICATION_NAME", "").upper()
    if not app_name:
        logger.warning(
            "ATLAN_APPLICATION_NAME not set. Cannot auto-discover credentials. "
            "Set it in your .env file or environment."
        )
        return {}

    # Collect app-level defaults: E2E_{APP_NAME}_*
    app_prefix = f"E2E_{app_name}_"
    app_credentials = _collect_env_credentials(app_prefix)

    if app_credentials:
        logger.info(
            f"Auto-discovered {len(app_credentials)} credential fields "
            f"from {app_prefix}* env vars: {list(app_credentials.keys())}"
        )
    else:
        logger.warning(
            f"No {app_prefix}* environment variables found. "
            f"Set them in your .env file or environment."
        )

    # Check for scenario-specific overrides: E2E_{SCENARIO_NAME}_*
    if scenario_name:
        scenario_prefix = f"E2E_{scenario_name.upper()}_"
        scenario_credentials = _collect_env_credentials(scenario_prefix)

        if scenario_credentials:
            logger.info(
                f"Found {len(scenario_credentials)} scenario-specific credential "
                f"fields from {scenario_prefix}* env vars: "
                f"{list(scenario_credentials.keys())}"
            )
            # Scenario-specific vars override app-level defaults
            return {**app_credentials, **scenario_credentials}

    return app_credentials


def _collect_env_credentials(prefix: str) -> Dict[str, Any]:
    """Collect credentials from environment variables matching a prefix.

    Args:
        prefix: The env var prefix to match (e.g. "E2E_POSTGRES_").

    Returns:
        Dict[str, Any]: Credentials extracted from matching env vars.
    """
    credentials: Dict[str, Any] = {}

    for key, value in os.environ.items():
        if key.startswith(prefix):
            field_name = key[len(prefix) :].lower()
            # Auto-convert numeric values (e.g., port)
            if value.isdigit():
                value = int(value)
            credentials[field_name] = value

    return credentials


def _auto_discover_server() -> str:
    """Auto-discover the app server URL from environment variables.

    Reads ATLAN_APP_HTTP_HOST and ATLAN_APP_HTTP_PORT to build the server URL.

    Returns:
        str: The server URL (e.g., "http://localhost:8000").
    """
    host = os.getenv("ATLAN_APP_HTTP_HOST", "localhost")
    port = os.getenv("ATLAN_APP_HTTP_PORT", "8000")
    # 0.0.0.0 isn't reachable from the test client, use localhost instead
    if host == "0.0.0.0":
        host = "localhost"
    return f"http://{host}:{port}"


def _check_server_health(server_url: str, timeout: int = 5) -> bool:
    """Check if the application server is running and reachable.

    Args:
        server_url: The server URL to check.
        timeout: Timeout in seconds.

    Returns:
        bool: True if server is reachable, False otherwise.
    """
    try:
        response = http_requests.get(f"{server_url}/server/health", timeout=timeout)
        return response.status_code == 200
    except http_requests.ConnectionError:
        return False
    except Exception:
        # Server is reachable but health endpoint might not exist
        # That's OK - at least the server is running
        return True


class BaseIntegrationTest:
    """Base class for integration tests.

    Subclasses just define scenarios and the framework handles everything else:
    credentials from env vars, server discovery, test method generation,
    and assertion validation.

    Minimal Example:
        >>> class TestMyConnector(BaseIntegrationTest):
        ...     scenarios = [
        ...         Scenario(name="auth_works", api="auth",
        ...                  assert_that={"success": equals(True)})
        ...     ]

    Class Attributes:
        scenarios: List of Scenario objects to execute.
        server_host: Base URL of the app server (auto-discovered from env if not set).
        server_version: API version prefix (default: "v1").
        workflow_endpoint: Default endpoint for workflow API (default: "/start").
        timeout: Request timeout in seconds (default: 30).
        default_credentials: Extra credential fields merged with auto-discovered ones.
        default_metadata: Default metadata for preflight/workflow tests.
        default_connection: Default connection info for workflow tests.
        skip_server_check: Set True to skip the server health check.
        extracted_output_base_path: Base directory where connector writes extracted
            output. Used for metadata output validation when scenarios set expected_data.

    Hooks:
        setup_test_environment: Called before any tests run.
        cleanup_test_environment: Called after all tests complete.
        build_credentials: Transform auto-discovered credentials before use.
        before_scenario: Called before each scenario.
        after_scenario: Called after each scenario.
    """

    # Scenario definitions - subclasses should override
    scenarios: List[Scenario] = []

    # Server configuration (auto-discovered from env if not set)
    server_host: str = ""
    server_version: str = "v1"
    workflow_endpoint: str = "/start"
    timeout: int = 30

    # Default values merged with auto-discovered credentials
    default_credentials: Dict[str, Any] = {}
    default_metadata: Dict[str, Any] = {}
    default_connection: Dict[str, Any] = {}

    # Skip server health check (useful for debugging)
    skip_server_check: bool = False

    # Base path for extracted output (used by metadata output validation)
    extracted_output_base_path: str = ""

    # Internal state
    client: IntegrationTestClient
    _results: List[ScenarioResult]

    def __init_subclass__(cls, **kwargs):
        """Auto-generate individual test methods for each scenario.

        This runs when a subclass is defined, creating test_<scenario_name>
        methods so pytest shows each scenario as a separate test.
        """
        super().__init_subclass__(**kwargs)

        # Only generate if the subclass defines its own scenarios
        if "scenarios" in cls.__dict__ and cls.scenarios:
            _generate_individual_tests(cls)

    @classmethod
    def setup_class(cls) -> None:
        """Set up the test class before any tests run.

        This method:
        1. Auto-discovers server URL and credentials from env vars
        2. Checks if the server is running
        3. Initializes the API client
        4. Calls the setup_test_environment hook
        """
        # Auto-discover server URL if not explicitly set
        if not cls.server_host:
            cls.server_host = _auto_discover_server()
            logger.info(f"Auto-discovered server: {cls.server_host}")

        # Server health check
        if not cls.skip_server_check:
            if not _check_server_health(cls.server_host):
                pytest.fail(
                    f"\n{'=' * 60}\n"
                    f"SERVER NOT RUNNING at {cls.server_host}\n"
                    f"{'=' * 60}\n"
                    f"Start the application server before running integration tests:\n"
                    f"  uv run python main.py\n"
                    f"{'=' * 60}"
                )

        # Validate that app-level credentials are discoverable (early warning)
        _auto_discover_credentials()

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
        """Tear down the test class after all tests complete."""
        logger.info(f"Tearing down integration test class: {cls.__name__}")

        # Call user-defined cleanup hook
        if hasattr(cls, "cleanup_test_environment"):
            logger.info("Running cleanup_test_environment hook")
            cls.cleanup_test_environment()

        # Log summary
        if cls._results:
            passed = sum(1 for r in cls._results if r.success)
            failed = sum(1 for r in cls._results if not r.success)
            total = len(cls._results)
            logger.info(
                f"Integration test summary: {passed}/{total} passed"
                + (f", {failed} failed" if failed else "")
            )

            # Write machine-readable summary for CI
            try:
                cls._write_summary()
            except Exception as e:
                logger.warning(f"Failed to write test summary: {e}")

    @classmethod
    def _write_summary(cls) -> Optional[str]:
        """Write a machine-readable JSON summary of all test results.

        Writes to the path specified by INTEGRATION_TEST_SUMMARY_PATH env var,
        defaulting to ./integration-test-summary.json.

        Returns:
            The path the summary was written to, or None if no results.
        """
        if not cls._results:
            return None

        summary_path = os.getenv(
            "INTEGRATION_TEST_SUMMARY_PATH",
            "./integration-test-summary.json",
        )

        passed = sum(1 for r in cls._results if r.success)
        failed = sum(
            1
            for r in cls._results
            if not r.success
            and r.error
            and not isinstance(r.error, pytest.skip.Exception)
        )
        skipped = sum(
            1
            for r in cls._results
            if r.error and isinstance(r.error, pytest.skip.Exception)
        )
        total = len(cls._results)

        scenarios_data = []
        for result in cls._results:
            scenario_entry: Dict[str, Any] = {
                "name": result.scenario.name,
                "api": result.scenario.api,
                "status": "passed"
                if result.success
                else (
                    "skipped"
                    if result.error and isinstance(result.error, pytest.skip.Exception)
                    else "failed"
                ),
                "duration_ms": round(result.duration_ms, 2),
                "description": result.scenario.description or "",
            }

            if result.assertion_results:
                scenario_entry["assertions"] = result.assertion_results

            if not result.success and result.error:
                scenario_entry["error"] = str(result.error)

            if result.scenario.expected_data:
                scenario_entry["metadata_validation"] = {
                    "expected_file": result.scenario.expected_data,
                    "strict": result.scenario.strict_comparison,
                }

            scenarios_data.append(scenario_entry)

        summary = {
            "app_name": os.getenv("ATLAN_APPLICATION_NAME", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "server_url": cls.server_host,
            "test_class": cls.__name__,
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "scenarios": scenarios_data,
        }

        summary_dir = os.path.dirname(os.path.abspath(summary_path))
        if summary_dir:
            os.makedirs(summary_dir, exist_ok=True)
        with open(summary_path, "wb") as f:
            f.write(orjson.dumps(summary, option=orjson.OPT_INDENT_2))

        logger.info(f"Integration test summary written to {summary_path}")
        return summary_path

    def _build_scenario_args(self, scenario: Scenario) -> Dict[str, Any]:
        """Build the API args for a scenario.

        Priority order for credentials:
        1. scenario.args (full override, backward compat) - used as-is
        2. scenario.credentials (explicit dict override)
        3. cls.default_credentials (class-level defaults)
        4. E2E_{SCENARIO_NAME}_* env vars (scenario-specific)
        5. E2E_{APP_NAME}_* env vars (app-level defaults)

        Args:
            scenario: The scenario to build args for.

        Returns:
            Dict[str, Any]: The complete args dict for the API call.
        """
        # If scenario has explicit args (backward compat), use those
        if scenario.args is not None:
            return evaluate_if_lazy(scenario.args)

        # Build credentials: env vars -> class defaults -> scenario overrides
        if scenario.credentials is not None:
            # Scenario provides explicit credentials - use as-is
            credentials = scenario.credentials
        else:
            # Discover credentials with scenario-specific overrides
            env_credentials = _auto_discover_credentials(scenario.name)
            # Merge env vars + class defaults
            credentials = {**env_credentials, **self.default_credentials}
            # Apply build_credentials hook if defined
            if hasattr(self, "build_credentials") and callable(self.build_credentials):
                credentials = self.build_credentials(credentials)

        args = {"credentials": credentials}

        # Add metadata for preflight and workflow
        if scenario.api.lower() in ("preflight", "workflow"):
            if scenario.metadata is not None:
                metadata = scenario.metadata
            else:
                metadata = {**self.default_metadata}
            args["metadata"] = metadata

        # Add connection for workflow
        if scenario.api.lower() == "workflow":
            if scenario.connection is not None:
                connection = scenario.connection
            else:
                connection = {**self.default_connection}
            args["connection"] = connection

        return args

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

            # Step 1: Build args (auto-fill from env if needed)
            args = self._build_scenario_args(scenario)
            logger.debug(f"Built args for {scenario.name}")

            # Step 2: Call the API
            endpoint = scenario.endpoint or self.workflow_endpoint
            response = self.client.call_api(
                api=scenario.api,
                args=args,
                endpoint_override=endpoint if scenario.api == "workflow" else None,
            )
            result.response = response
            logger.debug(f"API response for {scenario.name}: {response}")

            # Step 3: Validate assertions with rich error messages
            assertion_results = self._validate_assertions(
                response, scenario.assert_that
            )
            result.assertion_results = assertion_results

            # Check if all assertions passed
            all_passed = all(r["passed"] for r in assertion_results.values())
            result.success = all_passed

            if not all_passed:
                failed_details = []
                for path, detail in assertion_results.items():
                    if not detail["passed"]:
                        desc_suffix = ""
                        if detail.get("description"):
                            desc_suffix = f"\n    \u2192 {detail['description']}"
                        failed_details.append(
                            f"  - {path}: expected {detail['expected']}, "
                            f"got {detail['actual']!r}{desc_suffix}"
                        )
                error_msg = (
                    f"Assertions failed for scenario '{scenario.name}':\n"
                    + "\n".join(failed_details)
                )
                logger.error(error_msg)
                raise AssertionError(error_msg)

            # Step 4: Validate metadata output if expected_data is set
            if scenario.expected_data and scenario.api_type == APIType.WORKFLOW:
                self._validate_workflow_output(scenario, response)

            logger.info(f"Scenario {scenario.name} passed")

        except Exception as e:
            result.error = e
            result.success = False
            if not isinstance(e, (AssertionError, pytest.skip.Exception)):
                logger.error(f"Scenario {scenario.name} failed with error: {e}")
            raise

        finally:
            result.duration_ms = (time.time() - start_time) * 1000
            self._results.append(result)

            # Call after_scenario hook if defined
            if hasattr(self, "after_scenario"):
                self.after_scenario(scenario, result)

        return result

    def _validate_assertions(
        self,
        response: Dict[str, Any],
        assertions: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        """Validate all assertions against the response.

        Returns rich results with actual/expected values for error messages.

        Args:
            response: The API response dictionary.
            assertions: Dictionary mapping paths to predicates.

        Returns:
            Dict[str, Dict]: Dict mapping paths to {passed, actual, expected}.
        """
        results = {}

        for path, predicate in assertions.items():
            actual = self._get_nested_value(response, path)
            expected_desc = getattr(predicate, "__doc__", str(predicate))
            custom_desc = getattr(predicate, "description", None)

            try:
                passed = predicate(actual)
                result_entry = {
                    "passed": passed,
                    "actual": actual,
                    "expected": expected_desc,
                }
                if custom_desc:
                    result_entry["description"] = custom_desc
                results[path] = result_entry
                if not passed:
                    logger.debug(
                        f"Assertion failed: {path} - "
                        f"expected {expected_desc}, got {actual!r}"
                    )
            except Exception as e:
                logger.error(f"Assertion error for {path}: {e}")
                result_entry = {
                    "passed": False,
                    "actual": actual,
                    "expected": expected_desc,
                    "error": str(e),
                }
                if custom_desc:
                    result_entry["description"] = custom_desc
                results[path] = result_entry

        return results

    def _validate_workflow_output(
        self, scenario: Scenario, response: Dict[str, Any]
    ) -> None:
        """Validate workflow output against expected metadata baseline.

        Polls for workflow completion, loads actual and expected output,
        and compares them to produce a gap report.

        Args:
            scenario: The scenario with expected_data set.
            response: The workflow start API response containing workflow_id/run_id.

        Raises:
            AssertionError: If metadata validation fails.
        """
        # Extract workflow_id and run_id from response
        data = response.get("data", {})
        workflow_id = data.get("workflow_id")
        run_id = data.get("run_id")

        if not workflow_id or not run_id:
            raise AssertionError(
                f"Cannot validate workflow output for scenario '{scenario.name}': "
                f"response missing workflow_id or run_id"
            )

        # Poll for workflow completion
        logger.info(
            f"Waiting for workflow completion: {workflow_id}/{run_id} "
            f"(timeout={scenario.workflow_timeout}s)"
        )
        final_status = self._poll_workflow_completion(
            workflow_id=workflow_id,
            run_id=run_id,
            timeout=scenario.workflow_timeout,
            interval=scenario.polling_interval,
        )

        if final_status != "COMPLETED":
            raise AssertionError(
                f"Workflow did not complete successfully for scenario "
                f"'{scenario.name}': status={final_status}"
            )

        # Resolve extracted output base path
        base_path = (
            scenario.extracted_output_base_path or self.extracted_output_base_path
        )
        if not base_path:
            raise AssertionError(
                f"Cannot validate workflow output for scenario '{scenario.name}': "
                f"extracted_output_base_path not set on scenario or test class"
            )

        # Load actual and expected data
        logger.info(f"Loading actual output from {base_path}/{workflow_id}/{run_id}")
        actual = load_actual_output(
            base_path,
            workflow_id,
            run_id,
            subdirectory=scenario.output_subdirectory,
        )

        logger.info(f"Loading expected data from {scenario.expected_data}")
        expected = load_expected_data(scenario.expected_data)

        # Compare
        gap_report = compare_metadata(
            expected=expected,
            actual=actual,
            strict=scenario.strict_comparison,
            ignored_fields=scenario.ignored_fields,
            expected_file=scenario.expected_data,
        )

        if gap_report.has_gaps:
            raise AssertionError(
                f"Metadata validation failed for scenario '{scenario.name}' "
                f"(baseline: {scenario.expected_data}):\n\n"
                + gap_report.format_report()
            )

        logger.info(
            f"Metadata validation passed for scenario '{scenario.name}': "
            f"{len(actual)} assets match expected baseline"
        )

    def _poll_workflow_completion(
        self,
        workflow_id: str,
        run_id: str,
        timeout: int,
        interval: int,
    ) -> str:
        """Poll the workflow status until completion or timeout.

        Args:
            workflow_id: The workflow ID.
            run_id: The run ID.
            timeout: Maximum seconds to wait.
            interval: Seconds between polls.

        Returns:
            str: The final workflow status (e.g., "COMPLETED", "FAILED").

        Raises:
            TimeoutError: If the workflow does not complete within the timeout.
        """
        start_time = time.time()

        while True:
            status_response = self.client.get_workflow_status(workflow_id, run_id)

            if not status_response.get("success", False):
                logger.warning(f"Workflow status check failed: {status_response}")
                # Continue polling — transient failures are possible
            else:
                current_status = status_response.get("data", {}).get("status", "")
                logger.debug(f"Workflow status: {current_status}")

                if current_status != "RUNNING":
                    return current_status

            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"Workflow {workflow_id}/{run_id} did not complete "
                    f"within {timeout}s (elapsed: {elapsed:.0f}s)"
                )

            time.sleep(interval)

    def _get_nested_value(self, data: Dict[str, Any], path: str) -> Any:
        """Get a value from a nested dictionary using dot notation.

        Args:
            data: The dictionary to search.
            path: Dot-separated path (e.g., "data.workflow_id").

        Returns:
            Any: The value at the path, or None if not found.
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

    # Keep test_scenarios for backward compat but skip if individual tests exist
    def test_scenarios(self) -> None:
        """Execute all scenarios (backward compatibility).

        When __init_subclass__ auto-generates individual test methods,
        this method becomes a no-op to avoid running scenarios twice.
        """
        # If individual test methods were generated, skip this
        has_individual = any(
            hasattr(self.__class__, f"test_{s.name}") for s in self.scenarios
        )
        if has_individual:
            return

        # Backward compat: run all scenarios in one test
        if not self.scenarios:
            pytest.skip("No scenarios defined")

        for scenario in self.scenarios:
            self._execute_scenario(scenario)


def _generate_individual_tests(test_class: Type[BaseIntegrationTest]) -> None:
    """Generate individual test methods for each scenario on the class.

    Each scenario becomes test_<scenario_name> so pytest shows them separately.

    Args:
        test_class: The test class to add methods to.
    """
    for scenario in test_class.scenarios:
        method_name = f"test_{scenario.name}"

        def make_test(s: Scenario):
            def test_method(self):
                self._execute_scenario(s)

            test_method.__doc__ = s.description or f"Test scenario: {s.name}"
            return test_method

        setattr(test_class, method_name, make_test(scenario))


# =============================================================================
# Public API (backward compat)
# =============================================================================


def generate_test_methods(test_class: Type[BaseIntegrationTest]) -> None:
    """Generate individual test methods for each scenario.

    NOTE: This is now done automatically via __init_subclass__ when you
    define scenarios on your test class. You don't need to call this manually.

    Args:
        test_class: The test class to add methods to.
    """
    _generate_individual_tests(test_class)


def parametrize_scenarios(scenarios: List[Scenario]):
    """Create a pytest parametrize decorator for scenarios.

    Args:
        scenarios: List of scenarios to parametrize.

    Returns:
        A pytest.mark.parametrize decorator.
    """
    return pytest.mark.parametrize(
        "scenario",
        scenarios,
        ids=[s.name for s in scenarios],
    )
