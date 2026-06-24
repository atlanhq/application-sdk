"""Execution layer for running Apps on Temporal."""

# Re-export the temporalio Client as TemporalClient for app-side type annotations.
from temporalio.client import Client as TemporalClient

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
    "TemporalAuthConfig",
    "TemporalAuthManager",
    "TemporalClient",
    "TemporalExecutorBackend",
    "build_output_path",
    "create_data_converter",
    "create_data_converter_for_app",
    "create_temporal_client",
    "create_worker",
    "get_object_store_prefix",
    "needs_lock",
]
