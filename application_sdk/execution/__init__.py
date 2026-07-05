"""Execution layer for running Apps on Temporal."""

# Re-export the temporalio Client as TemporalClient for app-side type annotations.
from temporalio.client import Client as TemporalClient
from temporalio.client import WorkflowFailureError as TemporalWorkflowFailureError

# Re-export the client-side failure types apps need to catch around
# `TemporalClient.execute_workflow(...)`. Renamed with a `Temporal` prefix (matching
# `TemporalClient`) so they don't collide with unrelated SDK types of the same short
# name, e.g. `application_sdk.common.error_codes.ActivityError`.
from temporalio.exceptions import ActivityError as TemporalActivityError

from application_sdk.execution._temporal.activity_utils import (
    build_output_path,
    get_object_store_prefix,
)
from application_sdk.execution._temporal.auth import (
    TemporalAuthConfig,
    TemporalAuthManager,
)
from application_sdk.execution._temporal.backend import (
    TemporalExecutorBackend,
    create_temporal_client,
)
from application_sdk.execution._temporal.converter import (
    create_data_converter,
    create_data_converter_for_app,
)
from application_sdk.execution._temporal.worker import AppWorker, create_worker
from application_sdk.execution.decorators import needs_lock
from application_sdk.execution.errors import ApplicationError
from application_sdk.execution.retry import RetryPolicy

__all__ = [
    "AppWorker",
    "ApplicationError",
    "RetryPolicy",
    "TemporalActivityError",
    "TemporalAuthConfig",
    "TemporalAuthManager",
    "TemporalClient",
    "TemporalExecutorBackend",
    "TemporalWorkflowFailureError",
    "build_output_path",
    "create_data_converter",
    "create_data_converter_for_app",
    "create_temporal_client",
    "create_worker",
    "get_object_store_prefix",
    "needs_lock",
]
