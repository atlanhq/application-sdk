"""Typed error leaves for the contracts types module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError


@dataclass(kw_only=True)
class RunPrefixRequiredError(InvalidInputError):
    """run_prefix is required for RETAINED-tier StorageTier operations."""

    code: ClassVar[str] = "INVALID_INPUT_RUN_PREFIX_REQUIRED"
    message: str = "run_prefix is required for RETAINED-tier operations"
    field: str | None = "run_prefix"
