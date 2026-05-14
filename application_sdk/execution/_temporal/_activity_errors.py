"""Typed error leaves for Temporal activity utilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import DependencyUnavailableError, InternalError


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
