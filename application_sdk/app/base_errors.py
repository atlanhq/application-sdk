"""Typed error leaves for the App and AppContext families."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    InternalError,
    PreconditionError,
    UnimplementedError,
)


@dataclass(kw_only=True)
class ObjectStoreNotConfiguredError(PreconditionError):
    """Object store required by a @task but not configured in the deployment."""

    code: ClassVar[str] = "PRECONDITION_OBJECT_STORE_NOT_CONFIGURED"
    message: str = (
        "No object store configured. "
        "Ensure the deployment has a storage binding or APP_STORAGE_ROOT set."
    )
    resource: str | None = "object_store"
    expected_state: str | None = "configured"


@dataclass(kw_only=True)
class UpstreamObjectStoreNotConfiguredError(PreconditionError):
    """ENABLE_ATLAN_UPLOAD is on, but the upstream object store did not resolve."""

    code: ClassVar[str] = "PRECONDITION_UPSTREAM_OBJECT_STORE_NOT_CONFIGURED"
    message: str = (
        "ENABLE_ATLAN_UPLOAD is true but no upstream object store resolved. "
        "Falling back to the deployment store would report success while the "
        "artifacts never reach Atlan's bucket. Check the Dapr component named "
        "by UPSTREAM_OBJECT_STORE_NAME and the secrets it references."
    )
    resource: str | None = "upstream_object_store"
    expected_state: str | None = "configured"


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
class SecretStoreNotConfiguredError(PreconditionError):
    """Secret store required by get_secret / resolve_credential but not configured."""

    code: ClassVar[str] = "PRECONDITION_SECRET_STORE_NOT_CONFIGURED"
    message: str = "No secret store configured"
    resource: str | None = "secret_store"
    expected_state: str | None = "configured"


@dataclass(kw_only=True)
class AbstractRunNotImplementedError(UnimplementedError):
    """App.run() called but the subclass has not implemented it."""

    code: ClassVar[str] = "UNIMPLEMENTED_APP_RUN"
    app_class: str | None = None
    message: str = "App must implement run() or define @entrypoint methods"
