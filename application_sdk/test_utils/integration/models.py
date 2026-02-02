"""Core data models for integration testing framework.

This module defines the data structures used to declare integration test scenarios
in a declarative, data-driven manner.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional, Union


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

    A scenario defines:
    - What API to test (auth, preflight, workflow)
    - What inputs to provide (can be lazy-evaluated)
    - What outputs to expect (using assertion predicates)

    Attributes:
        name: Unique identifier for the scenario.
        api: The API type to test ("auth", "preflight", "workflow").
        args: Input arguments for the API call. Can be a dict or Lazy wrapper.
        assert_that: Dictionary mapping response paths to assertion predicates.
        endpoint: Optional override for the workflow endpoint (dynamic).
        description: Optional human-readable description of what this tests.
        skip: If True, this scenario will be skipped during test execution.
        skip_reason: Reason for skipping (shown in test output).

    Example:
        >>> from application_sdk.test_utils.integration import Scenario, equals, lazy
        >>> scenario = Scenario(
        ...     name="auth_valid_credentials",
        ...     api="auth",
        ...     args=lazy(lambda: {"credentials": load_creds()}),
        ...     assert_that={"success": equals(True)},
        ...     description="Test authentication with valid credentials"
        ... )
    """

    name: str
    api: str
    args: Union[Dict[str, Any], LazyValue]
    assert_that: Dict[str, Predicate]
    endpoint: Optional[str] = None
    description: str = ""
    skip: bool = False
    skip_reason: str = ""

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

    @property
    def api_type(self) -> APIType:
        """Get the API type as an enum value.

        Returns:
            APIType: The API type enum.
        """
        return APIType.from_string(self.api)


@dataclass
class ScenarioResult:
    """Result of executing a single scenario.

    Attributes:
        scenario: The scenario that was executed.
        success: Whether all assertions passed.
        response: The raw API response.
        assertion_results: Dictionary mapping assertion paths to pass/fail status.
        error: Exception if the scenario failed unexpectedly.
        duration_ms: Time taken to execute the scenario in milliseconds.
    """

    scenario: Scenario
    success: bool
    response: Optional[Dict[str, Any]] = None
    assertion_results: Dict[str, bool] = field(default_factory=dict)
    error: Optional[Exception] = None
    duration_ms: float = 0.0

    def __str__(self) -> str:
        """Human-readable representation of the result."""
        status = "PASSED" if self.success else "FAILED"
        msg = f"[{status}] {self.scenario.name}"
        if not self.success and self.error:
            msg += f" - Error: {self.error}"
        elif not self.success:
            failed = [k for k, v in self.assertion_results.items() if not v]
            msg += f" - Failed assertions: {failed}"
        return msg
