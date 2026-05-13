"""Core data models for integration testing framework.

This module defines the data structures used to declare integration test scenarios
in a declarative, data-driven manner.
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from application_sdk.errors import InvalidInputError
from application_sdk.testing.integration.assertions import Predicate


class APIType(Enum):
    """Supported API types for integration testing.

    These represent the Core APIs that connectors implement.
    """

    AUTH = "auth"
    METADATA = "metadata"
    PREFLIGHT = "preflight"
    WORKFLOW = "workflow"
    CONFIG = "config"

    @classmethod
    def from_string(cls, value: str) -> "APIType":
        """Convert string to APIType enum.

        Args:
            value: String representation of the API type.

        Returns:
            APIType: The corresponding enum value.

        Raises:
            ValueError: If the value is not a valid API type.
        """
        try:
            return cls(value.lower())
        except ValueError:
            valid_values = [e.value for e in cls]
            raise InvalidInputError(
                message=f"Invalid API type: '{value}'. Must be one of: {valid_values}",
                field="api_type",
                value_summary=value,
            )


# Type alias for lazy evaluation wrapper (forward reference)
LazyValue = Any  # Will be Lazy type from lazy.py


@dataclass
class Scenario:
    """Represents a single integration test scenario.

    A scenario defines what API to test and what to assert on the response.
    Credentials, metadata, and connection are auto-loaded from environment
    variables unless explicitly overridden.

    Simplified Usage (recommended):
        >>> Scenario(
        ...     name="auth_works",
        ...     api="auth",
        ...     assert_that={"success": equals(True)},
        ... )
        # Credentials auto-loaded from E2E_{APP_NAME}_* env vars

    Override credentials for negative tests:
        >>> Scenario(
        ...     name="auth_fails",
        ...     api="auth",
        ...     credentials={"username": "bad", "password": "wrong"},
        ...     assert_that={"success": equals(False)},
        ... )

    Override metadata for preflight:
        >>> Scenario(
        ...     name="preflight_custom",
        ...     api="preflight",
        ...     metadata={"include-filter": '{"^mydb$": ["^public$"]}'},
        ...     assert_that={"success": equals(True)},
        ... )

    Attributes:
        name: Unique identifier for the scenario.
        api: The API type to test ("auth", "metadata", "preflight", "workflow", "config").
        assert_that: Dictionary mapping response paths to assertion predicates.
        credentials: Optional credentials override. If not provided, auto-loaded from env.
        metadata: Optional metadata override. If not provided, uses class defaults.
        connection: Optional connection override. If not provided, uses class defaults.
        args: Full args override for backward compatibility. Takes precedence over
              credentials/metadata/connection if provided.
        endpoint: Optional override for the workflow endpoint (dynamic).
        description: Optional human-readable description of what this tests.
        skip: If True, this scenario will be skipped during test execution.
        skip_reason: Reason for skipping (shown in test output).
        expected_data: Optional path to a JSON file containing expected metadata output.
            When set, the framework will poll for workflow completion and compare
            actual extracted metadata against the expected baseline.
        extracted_output_base_path: Optional base directory where connector writes
            extracted output. Falls back to the class-level attribute if not set.
        output_subdirectory: Subdirectory within the run output dir to search for
            JSONL files. Defaults to "transformed". Set to "" to search the
            entire run directory.
        strict_comparison: If True, extra assets in actual output that are not in
            the expected JSON will cause the test to fail. Defaults to True.
        workflow_timeout: Seconds to wait for workflow completion. Defaults to 300.
        polling_interval: Seconds between workflow status polls. Defaults to 10.
        ignored_fields: Set of attribute field names to skip during comparison
            (e.g., dynamic fields like qualifiedName that change between runs).
            If not provided, a default set of dynamic fields is used.
        config_action: For config API scenarios, "get" or "update".
        config_workflow_id: Workflow ID for config GET/POST. Can be a string
            or a callable (e.g., lambda that reads from shared state).
        config_payload: For config update, the payload to send.
        schema_base_path: Base directory containing pandera YAML schemas for
            data validation. When set, extracted output files are validated
            against the pandera schemas after workflow completion.
        connection_config: Optional connection configuration for v3 preflight/metadata
            endpoints. Maps to PreflightInput.connection_config. Falls back to
            ``metadata`` if not provided (backward compat with v2 scenarios).
        checks_to_run: Optional list of specific preflight checks to run.
            Maps to PreflightInput.checks_to_run. Empty list = run all checks.
        preflight_timeout: Timeout in seconds for preflight checks.
            Maps to PreflightInput.timeout_seconds. Defaults to 60.
    """

    name: str
    api: str
    assert_that: dict[str, Predicate]
    credentials: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    connection: dict[str, Any] | None = None
    args: dict[str, Any] | LazyValue | None = None
    endpoint: str | None = None
    description: str = ""
    skip: bool = False
    skip_reason: str = ""
    expected_data: str | None = None
    extracted_output_base_path: str | None = None
    output_subdirectory: str = "transformed"
    strict_comparison: bool = True
    workflow_timeout: int = 300
    polling_interval: int = 10
    ignored_fields: set[str] | None = None
    config_action: str | None = None
    config_workflow_id: Any | None = None
    config_payload: dict[str, Any] | None = None
    schema_base_path: str | None = None
    connection_config: dict[str, Any] | None = None
    checks_to_run: list[str] | None = None
    preflight_timeout: int = 60

    def __post_init__(self):
        """Validate the scenario after initialization."""
        if not self.name:
            raise InvalidInputError(
                message="Scenario name cannot be empty",
                field="name",
            )

        if not self.api:
            raise InvalidInputError(
                message="Scenario api cannot be empty",
                field="api",
            )

        # Validate API type
        valid_apis = [e.value for e in APIType]
        if self.api.lower() not in valid_apis:
            raise InvalidInputError(
                message=f"Invalid API type: '{self.api}'. Must be one of: {valid_apis}",
                field="api",
                value_summary=self.api,
            )

        if not self.assert_that:
            raise InvalidInputError(
                message="Scenario must have at least one assertion",
                field="assert_that",
            )

        if self.expected_data and self.api.lower() != "workflow":
            raise InvalidInputError(
                message=f"expected_data can only be set for workflow scenarios, but api is '{self.api}'",
                field="expected_data",
            )

        if self.expected_data and not os.path.isfile(self.expected_data):
            raise FileNotFoundError(
                f"Scenario '{self.name}': expected_data file not found: "
                f"{self.expected_data}"
            )

        if self.api.lower() == "config":
            if self.config_action not in ("get", "update"):
                raise InvalidInputError(
                    message=f"config_action must be 'get' or 'update' for config scenarios, got: {self.config_action!r}",
                    field="config_action",
                    value_summary=str(self.config_action),
                )
            if self.config_workflow_id is None:
                raise InvalidInputError(
                    message="config_workflow_id is required for config scenarios",
                    field="config_workflow_id",
                )

    @property
    def api_type(self) -> APIType:
        """Get the API type as an enum value."""
        return APIType.from_string(self.api)

    @property
    def uses_default_credentials(self) -> bool:
        """Check if this scenario uses default (auto-loaded) credentials."""
        return self.credentials is None and self.args is None


@dataclass
class ScenarioResult:
    """Result of executing a single scenario.

    Attributes:
        scenario: The scenario that was executed.
        success: Whether all assertions passed.
        response: The raw API response.
        assertion_results: Dictionary mapping assertion paths to pass/fail details.
        error: Exception if the scenario failed unexpectedly.
        duration_ms: Time taken to execute the scenario in milliseconds.
    """

    scenario: Scenario
    success: bool
    response: dict[str, Any] | None = None
    assertion_results: dict[str, Any] = field(default_factory=dict)
    error: Exception | None = None
    duration_ms: float = 0.0

    def __str__(self) -> str:
        """Human-readable representation of the result."""
        status = "PASSED" if self.success else "FAILED"
        msg = f"[{status}] {self.scenario.name}"
        if not self.success and self.error:
            msg += f" - Error: {self.error}"
        elif not self.success:
            failed = [
                k
                for k, v in self.assertion_results.items()
                if not v.get("passed", False)
            ]
            msg += f" - Failed assertions: {failed}"
        return msg
