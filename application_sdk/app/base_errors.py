"""Typed error leaves for the App and AppContext families."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk._context_errors import (  # re-exported; defined in the api distribution
    ObjectStoreNotConfiguredError,
    SecretStoreNotConfiguredError,
    UpstreamObjectStoreNotConfiguredError,
)
from application_sdk.errors.leaves import (
    InternalError,
    PreconditionError,
    UnimplementedError,
)

__all__ = [
    "AbstractRunNotImplementedError",
    "ObjectStoreNotConfiguredError",
    "SecretStoreNotConfiguredError",
    "SingleStoreUnknownInWorkflowError",
    "StateStoreNotConfiguredError",
    "UpstreamObjectStoreNotConfiguredError",
]


@dataclass(kw_only=True)
class SingleStoreUnknownInWorkflowError(InternalError):
    """``AppContext.single_store`` read from workflow code, which holds no stores.

    The workflow-side ``AppContext`` (``app/base.py``, the ``_run`` wrapper) is
    built without object-store handles — only activities receive them from
    ``InfrastructureContext``.  ``is_single_store(None, None)`` is ``True``, so
    every deployment would look single-store there, including a real SDR one
    with two distinct buckets.  Raise instead of answering.
    """

    code: ClassVar[str] = "INTERNAL_SINGLE_STORE_UNKNOWN_IN_WORKFLOW"
    message: str = (
        "AppContext.single_store is not answerable in workflow code: the "
        "workflow-side context carries no object-store handles, so every "
        "deployment would read as single-store — including a two-store SDR "
        "deployment."
    )
    component: str | None = "app.context"
    invariant: str | None = "single_store is only read where the store handles exist"
    suggested_action: str | None = (
        "Read it inside a @task and return the verdict on that task's typed "
        "Output, then branch on that value in workflow code. A replayed "
        "workflow then sees the value the original run recorded."
    )


@dataclass(kw_only=True)
class StateStoreNotConfiguredError(PreconditionError):
    """State store required by save_state / load_state but not configured."""

    code: ClassVar[str] = "PRECONDITION_STATE_STORE_NOT_CONFIGURED"
    message: str = "No state store configured"
    resource: str | None = "state_store"
    expected_state: str | None = "configured"


@dataclass(kw_only=True)
class AbstractRunNotImplementedError(UnimplementedError):
    """App.run() called but the subclass has not implemented it."""

    code: ClassVar[str] = "UNIMPLEMENTED_APP_RUN"
    app_class: str | None = None
    message: str = "App must implement run() or define @entrypoint methods"
