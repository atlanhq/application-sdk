"""Typed precondition error subclasses for missing store configurations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import PreconditionError


@dataclass(kw_only=True)
class NoStateStoreError(PreconditionError):
    """No state store configured — call App.load() with a state store first."""

    code: ClassVar[str] = "PRECONDITION_NO_STATE_STORE"


@dataclass(kw_only=True)
class NoSecretStoreError(PreconditionError):
    """No secret store configured — call App.load() with a secret store first."""

    code: ClassVar[str] = "PRECONDITION_NO_SECRET_STORE"


@dataclass(kw_only=True)
class NoObjectStoreError(PreconditionError):
    """No object store configured — call App.load() with an object store first."""

    code: ClassVar[str] = "PRECONDITION_NO_OBJECT_STORE"
