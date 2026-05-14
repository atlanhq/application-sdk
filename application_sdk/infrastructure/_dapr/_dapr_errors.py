"""Typed error leaves for the Dapr infrastructure client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import DependencyUnavailableError


@dataclass(kw_only=True)
class SecretFetchError(DependencyUnavailableError):
    """A secret could not be fetched from the Dapr secret store."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_SECRET_FETCH"
    message: str = "Failed to fetch secret"
    service: str | None = "dapr"
    component: str | None = None
