"""Typed error leaves for the Dapr infrastructure client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import UnimplementedError


@dataclass(kw_only=True)
class DaprListKeysUnsupportedError(UnimplementedError):
    """Key listing is not supported by this Dapr state-store backend."""

    code: ClassVar[str] = "UNIMPLEMENTED_DAPR_LIST_KEYS"
    message: str = "Key listing is not supported by all Dapr state stores"
    operation: str | None = "list_keys"
    reason: str | None = "dapr_state_store_no_list"
