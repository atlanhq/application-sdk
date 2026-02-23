"""Core data models for integration testing framework.

This module defines the data structures used to declare integration test scenarios
in a declarative, data-driven manner.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional, Set, Union


class APIType(Enum):
    """Supported API types for integration testing.

    These represent the Core 3 APIs that every connector must implement.
    """

    AUTH = "auth"
    PREFLIGHT = "preflight"
    WORKFLOW = "workflow"

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
            raise ValueError(
                f"Invalid API type: '{value}'. Must be one of: {valid_values}"
            )


# Type alias for assertion predicates (higher-order functions)
Predicate = Callable[[Any], bool]

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
        api: The API type to test ("auth", "preflight", "workflow").
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
        strict_comparison: If True, extra assets in actual output that are not in
            the expected JSON will cause the test to fail. Defaults to True.
        workflow_timeout: Seconds to wait for workflow completion. Defaults to 300.
        polling_interval: Seconds between workflow status polls. Defaults to 10.
        ignored_fields: Set of attribute field names to skip during comparison
            (e.g., dynamic fields like qualifiedName that change between runs).
            If not provided, a default set of dynamic fields is used.
    """

    name: str
    api: str
    assert_that: Dict[str, Predicate]
    credentials: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    connection: Optional[Dict[str, Any]] = None
    args: Optional[Union[Dict[str, Any], LazyValue]] = None
    endpoint: Optional[str] = None
    description: str = ""
    skip: bool = False
    skip_reason: str = ""
    expected_data: Optional[str] = None
    extracted_output_base_path: Optional[str] = None
    strict_comparison: bool = True
    workflow_timeout: int = 300
    polling_interval: int = 10
    ignored_fields: Optional[Set[str]] = None

    def __post_init__(self):
        """Validate the scenario after initialization."""
        if not self.name:
            raise ValueError("Scenario name cannot be empty")

        if not self.api:
            raise ValueError("Scenario api cannot be empty")

        # Validate API type
        valid_apis = [e.value for e in APIType]
        if self.api.lower() not in valid_apis:
            raise ValueError(
                f"Invalid API type: '{self.api}'. Must be one of: {valid_apis}"
            )

        if not self.assert_that:
            raise ValueError("Scenario must have at least one assertion")

        if self.expected_data and self.api.lower() != "workflow":
            raise ValueError(
                "expected_data can only be set for workflow scenarios, "
                f"but api is '{self.api}'"
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
    response: Optional[Dict[str, Any]] = None
    assertion_results: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Exception] = None
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
