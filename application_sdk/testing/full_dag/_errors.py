"""Typed error leaves for the full-DAG end-to-end test harness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    AppTimeoutError,
    DataIntegrityError,
    DependencyUnavailableError,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
)

# ---------------------------------------------------------------------------
# Atlan API client errors (Family B)
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class AtlanApiHttpError(DependencyUnavailableError):
    """Non-2xx response from the Atlan Automation Engine API."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_ATLAN_API"
    service: str | None = "atlan_api"


@dataclass(kw_only=True)
class AtlanApiResponseInvariantError(DataIntegrityError):
    """AE API returned 2xx but the expected field (slug, run_id) was absent."""

    code: ClassVar[str] = "DATA_INTEGRITY_ATLAN_API_RESPONSE"
    location: str | None = "atlan_api_client"


@dataclass(kw_only=True)
class AtlanApiTimeoutError(AppTimeoutError):
    """No response received from the AE API before the timeout elapsed."""

    code: ClassVar[str] = "TIMEOUT_ATLAN_API"
    operation: str | None = "native_status_poll"


# ---------------------------------------------------------------------------
# Harness setup / precondition errors (Family C)
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class MissingHarnessClassAttrError(InvalidInputError):
    """A required class-level attribute was not set on the test harness."""

    code: ClassVar[str] = "INVALID_INPUT_HARNESS_CLASS_ATTR"


@dataclass(kw_only=True)
class MissingHarnessEnvError(InvalidInputError):
    """Required environment variables for the full-DAG harness are absent."""

    code: ClassVar[str] = "INVALID_INPUT_HARNESS_ENV"
    field: str | None = "ATLAN_BASE_URL,ATLAN_API_KEY"


@dataclass(kw_only=True)
class ManifestFileNotFoundError(NotFoundError):
    """The workflow manifest JSON file does not exist at the expected path."""

    code: ClassVar[str] = "NOT_FOUND_MANIFEST"
    resource_type: str | None = "manifest"


@dataclass(kw_only=True)
class ManifestDagMissingError(DataIntegrityError):
    """The manifest file exists but contains no top-level ``dag`` object."""

    code: ClassVar[str] = "DATA_INTEGRITY_MANIFEST_DAG_MISSING"
    expectation: str | None = "dag object present"


@dataclass(kw_only=True)
class AdminRoleNotResolvedError(PreconditionError):
    """The pyatlan role cache could not resolve the ``$admin`` role GUID."""

    code: ClassVar[str] = "PRECONDITION_ADMIN_ROLE_NOT_RESOLVED"
    expected_state: str | None = "role $admin present in role cache"


@dataclass(kw_only=True)
class AgentSpecRequiredError(InvalidInputError):
    """Agent mode requires an ``AgentSpec`` but none was provided."""

    code: ClassVar[str] = "INVALID_INPUT_AGENT_SPEC_REQUIRED"
    message: str = "Agent mode requires an AgentSpec"
    field: str | None = "agent_spec"
    constraint: str | None = "required_for_agent_mode"
