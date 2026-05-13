"""Typed data-integrity errors for atlas entity transformations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import DataIntegrityError


@dataclass(kw_only=True)
class EntityTransformError(DataIntegrityError):
    """Atlas entity failed invariant validation during transformation."""

    entity_type: str | None = None
    code: ClassVar[str] = "DATA_INTEGRITY_ENTITY_TRANSFORM"
