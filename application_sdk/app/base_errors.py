"""Typed error leaves for the App and AppContext families."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    InvalidInputError,
    PreconditionError,
    UnimplementedError,
)


@dataclass(kw_only=True)
class MapBatchedInvalidArgumentError(InvalidInputError):
    """App.map_batched() called with an invalid batch_size or max_concurrency."""

    code: ClassVar[str] = "INVALID_INPUT_MAP_BATCHED_ARGUMENT"


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
