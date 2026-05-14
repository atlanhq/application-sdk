"""Typed error leaves for Temporal activity utilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    DependencyUnavailableError,
    InternalError,
    InvalidInputError,
)


@dataclass(kw_only=True)
class WorkflowIdError(InternalError):
    """Workflow ID could not be retrieved from the current activity context."""

    code: ClassVar[str] = "INTERNAL_WORKFLOW_ID"
    message: str = "Failed to get workflow id"
    component: str | None = "temporal_activity"


@dataclass(kw_only=True)
class WorkflowRunIdError(InternalError):
    """Workflow run ID could not be retrieved from the current activity context."""

    code: ClassVar[str] = "INTERNAL_WORKFLOW_RUN_ID"
    message: str = "Failed to get workflow run id"
    component: str | None = "temporal_activity"


@dataclass(kw_only=True)
class EventPublishError(DependencyUnavailableError):
    """Event could not be published via the pub/sub binding."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_EVENT_PUBLISH"
    message: str = "Failed to publish event"
    service: str | None = "pubsub"


@dataclass(kw_only=True)
class TemporalAuthTokenAcquireError(DependencyUnavailableError):
    """Failed to acquire the initial Temporal OAuth auth token."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_TEMPORAL_AUTH"
    message: str = "Failed to acquire initial Temporal auth token"
    service: str | None = "temporal_auth"


@dataclass(kw_only=True)
class TemporalAuthConfigError(InvalidInputError):
    """Temporal auth configuration is missing a required URL field."""

    code: ClassVar[str] = "INVALID_INPUT_TEMPORAL_AUTH_CONFIG"
    message: str = "Either token_url or base_url must be set in TemporalAuthConfig"
    field: str | None = "token_url"


@dataclass(kw_only=True)
class WorkerInterceptorDuplicateError(InvalidInputError):
    """create_worker() received built-in interceptor types in the user-supplied list."""

    code: ClassVar[str] = "INVALID_INPUT_WORKER_INTERCEPTORS"
    message: str = "Duplicate built-in interceptor types supplied to create_worker"
    field: str | None = "interceptors"
