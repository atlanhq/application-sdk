"""Execution layer for running Apps on Temporal."""

# Re-export the temporalio Client as TemporalClient for app-side type annotations.
from typing import TYPE_CHECKING

from temporalio.client import Client as TemporalClient
from temporalio.client import WorkflowFailureError as TemporalWorkflowFailureError

# Re-export the client-side failure types apps need to catch around
# `TemporalClient.execute_workflow(...)`. Renamed with a `Temporal` prefix (matching
# `TemporalClient`) so they don't collide with unrelated SDK types of the same short
# name, e.g. `application_sdk.common.error_codes.ActivityError` and
# `application_sdk.errors.leaves.CancelledError`. `TemporalApplicationError` is the base
# temporalio class that failures are read back as; `ApplicationError` below is the
# SDK's subclass for raising, so match failure chains on `TemporalApplicationError`.
#
# These are raw temporalio exceptions for code awaiting `TemporalClient.execute_workflow(...)`
# or a workflow handle. For SDK-classified domain failures inside `@task`/`@entrypoint` code,
# raise/catch `application_sdk.errors.AppError` leaves (`CancelledError`, `AppTimeoutError`,
# etc.) instead.
from temporalio.exceptions import ActivityError as TemporalActivityError
from temporalio.exceptions import ApplicationError as TemporalApplicationError
from temporalio.exceptions import CancelledError as TemporalCancelledError
from temporalio.exceptions import ChildWorkflowError as TemporalChildWorkflowError
from temporalio.exceptions import TerminatedError as TemporalTerminatedError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

from application_sdk._install import lazy_getattr

# Eager: leaves with no SDK imports, which ``application_sdk.app`` imports
# (``RetryPolicy``) while it initialises.
from application_sdk.execution.errors import ApplicationError
from application_sdk.execution.retry import RetryPolicy, retry_product_seconds

if TYPE_CHECKING:
    from application_sdk.execution import progress as progress
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

# Lazy (FND-4260): the Temporal runtime imports ``application_sdk.app``, and
# ``application_sdk.app`` imports ``execution.retry``, which runs this
# ``__init__`` first. Importing the runtime eagerly here made the two packages
# one import cycle, so every module either side had to defer its imports into
# function bodies. The runtime now loads on first access.
#: Names served on first access: ``name -> (module, attribute)``.
_LAZY: dict[str, tuple[str, str]] = {
    "build_output_path": (
        "application_sdk.execution._temporal.activity_utils",
        "build_output_path",
    ),
    "get_object_store_prefix": (
        "application_sdk.execution._temporal.activity_utils",
        "get_object_store_prefix",
    ),
    "TemporalAuthConfig": (
        "application_sdk.execution._temporal.auth",
        "TemporalAuthConfig",
    ),
    "TemporalAuthManager": (
        "application_sdk.execution._temporal.auth",
        "TemporalAuthManager",
    ),
    "TemporalExecutorBackend": (
        "application_sdk.execution._temporal.backend",
        "TemporalExecutorBackend",
    ),
    "create_temporal_client": (
        "application_sdk.execution._temporal.backend",
        "create_temporal_client",
    ),
    "create_data_converter": (
        "application_sdk.execution._temporal.converter",
        "create_data_converter",
    ),
    "create_data_converter_for_app": (
        "application_sdk.execution._temporal.converter",
        "create_data_converter_for_app",
    ),
    "AppWorker": ("application_sdk.execution._temporal.worker", "AppWorker"),
    "create_worker": ("application_sdk.execution._temporal.worker", "create_worker"),
    "needs_lock": ("application_sdk.execution.decorators", "needs_lock"),
}

#: Submodules this package imported eagerly before it went lazy: still
#: attributes of the package on first access (``execution.progress``,
#: ``execution._temporal``).
_EAGER_SUBMODULES = frozenset({"_temporal", "decorators", "progress"})

__all__ = [
    "AppWorker",
    "ApplicationError",
    "RetryPolicy",
    "TemporalActivityError",
    "TemporalApplicationError",
    "TemporalAuthConfig",
    "TemporalAuthManager",
    "TemporalCancelledError",
    "TemporalChildWorkflowError",
    "TemporalClient",
    "TemporalExecutorBackend",
    "TemporalTerminatedError",
    "TemporalTimeoutError",
    "TemporalWorkflowFailureError",
    "build_output_path",
    "create_data_converter",
    "create_data_converter_for_app",
    "create_temporal_client",
    "create_worker",
    "get_object_store_prefix",
    "needs_lock",
    "retry_product_seconds",
]


def __getattr__(name: str) -> object:
    return lazy_getattr(__name__, name, _LAZY, globals(), _EAGER_SUBMODULES)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY) | _EAGER_SUBMODULES)
