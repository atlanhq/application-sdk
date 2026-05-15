"""Typed error leaves for the Dapr infrastructure client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import DependencyUnavailableError, UnimplementedError


@dataclass(kw_only=True)
class DaprListKeysUnsupportedError(UnimplementedError):
    """Key listing is not supported by this Dapr state-store backend."""

    code: ClassVar[str] = "UNIMPLEMENTED_DAPR_LIST_KEYS"
    message: str = "Key listing is not supported by all Dapr state stores"
    operation: str | None = "list_keys"
    reason: str | None = "dapr_state_store_no_list"


@dataclass(kw_only=True)
class SecretFetchError(DependencyUnavailableError):
    """A secret could not be fetched from the Dapr secret store."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_SECRET_FETCH"
    message: str = "Failed to fetch secret"
    service: str | None = "dapr"
    component: str | None = None
