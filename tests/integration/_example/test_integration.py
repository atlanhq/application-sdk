"""Example integration test class.

This file demonstrates how to create an integration test class
using the Apps-SDK integration testing framework.

Usage:
    # Set environment variables
    export EXAMPLE_DB_HOST=localhost
    export EXAMPLE_DB_USER=test
    export EXAMPLE_DB_PASSWORD=secret
    export APP_SERVER_URL=http://localhost:8000

    # Run tests
    pytest tests/integration/_example/ -v

    # Run with logging
    pytest tests/integration/_example/ -v --log-cli-level=INFO

    # Run specific scenario
    pytest tests/integration/_example/ -v -k "auth_valid"
"""

import os
import pytest

from application_sdk.test_utils.integration import (
    BaseIntegrationTest,
    parametrize_scenarios,
)

from .scenarios import (
    scenarios,
    auth_scenarios,
    preflight_scenarios,
    workflow_scenarios,
)


class ExampleIntegrationTest(BaseIntegrationTest):
    """Integration tests for the example connector.

    This class demonstrates the basic usage of the integration
    testing framework. Copy this class and modify it for your
    connector.

    Class Attributes:
        scenarios: List of all scenarios to run
        server_host: URL of the application server
        workflow_endpoint: Endpoint for starting workflows

    To customize:
        1. Change server_host to your server URL
        2. Change workflow_endpoint if different from "/start"
        3. Add setup_test_environment() for test data setup
        4. Add cleanup_test_environment() for cleanup
    """

    # All scenarios from scenarios.py
    scenarios = scenarios

    # Server configuration
    # These can be overridden by environment variables
    server_host = os.getenv("APP_SERVER_URL", "http://localhost:8000")
    server_version = "v1"
    workflow_endpoint = "/start"
    timeout = 30

    @classmethod
    def setup_test_environment(cls):
        """Set up test environment before any tests run.

        Override this method to:
        - Create test databases/schemas
        - Insert test data
        - Set up test fixtures

        Example:
            @classmethod
            def setup_test_environment(cls):
                # Create test schema
                cls.db_client = create_db_connection()
                cls.db_client.execute("CREATE SCHEMA IF NOT EXISTS test_schema")
                cls.db_client.execute("CREATE TABLE test_schema.test_table (...)")
        """
        # Uncomment and customize for your connector
        # print("Setting up test environment...")
        pass

    @classmethod
    def cleanup_test_environment(cls):
        """Clean up test environment after all tests complete.

        Override this method to:
        - Drop test databases/schemas
        - Remove test data
        - Clean up resources

        Example:
            @classmethod
            def cleanup_test_environment(cls):
                # Drop test schema
                cls.db_client.execute("DROP SCHEMA IF EXISTS test_schema CASCADE")
                cls.db_client.close()
        """
        # Uncomment and customize for your connector
        # print("Cleaning up test environment...")
        pass

    def before_scenario(self, scenario):
        """Called before each scenario runs.

        Override this for per-scenario setup.

        Args:
            scenario: The Scenario about to be executed.
        """
        # Uncomment to add per-scenario setup
        # print(f"About to run: {scenario.name}")
        pass

    def after_scenario(self, scenario, result):
        """Called after each scenario completes.

        Override this for per-scenario cleanup or logging.

        Args:
            scenario: The Scenario that was executed.
            result: The ScenarioResult with execution details.
        """
        # Uncomment to add per-scenario logging
        # status = "PASSED" if result.success else "FAILED"
        # print(f"{scenario.name}: {status} ({result.duration_ms:.2f}ms)")
        pass


# =============================================================================
# Alternative: Parametrized Test Class
# =============================================================================
# This approach creates a separate test for each scenario in pytest output


class ExampleParametrizedTest(BaseIntegrationTest):
    """Alternative test class using pytest parametrization.

    This creates a separate test entry for each scenario in pytest output,
    which can be helpful for CI/CD reporting.
    """

    server_host = os.getenv("APP_SERVER_URL", "http://localhost:8000")

    @parametrize_scenarios(auth_scenarios)
    def test_auth_scenarios(self, scenario):
        """Test all authentication scenarios."""
        self._execute_scenario(scenario)

    @parametrize_scenarios(preflight_scenarios)
    def test_preflight_scenarios(self, scenario):
        """Test all preflight scenarios."""
        self._execute_scenario(scenario)

    @parametrize_scenarios(workflow_scenarios)
    def test_workflow_scenarios(self, scenario):
        """Test all workflow scenarios."""
        self._execute_scenario(scenario)


# =============================================================================
# Alternative: Individual Test Methods
# =============================================================================
# Uncomment this section to generate individual test methods


# from application_sdk.test_utils.integration import generate_test_methods
#
# class ExampleIndividualTest(BaseIntegrationTest):
#     """Test class with individual methods per scenario."""
#     
#     scenarios = scenarios
#     server_host = os.getenv("APP_SERVER_URL", "http://localhost:8000")
#
# # Generate test_auth_valid_credentials, test_preflight_valid_config, etc.
# generate_test_methods(ExampleIndividualTest)
