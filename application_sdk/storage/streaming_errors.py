"""Typed error leaves for the record streaming helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError


@dataclass(kw_only=True)
class InvalidBatchBudgetError(InvalidInputError):
    """A byte budget for a batch admits no bytes at all."""

    code: ClassVar[str] = "INVALID_INPUT_BATCH_BUDGET"
    message: str = "A batch byte budget must be a positive number of bytes"
    field: str | None = "max_bytes"
    received_value: str | None = None
