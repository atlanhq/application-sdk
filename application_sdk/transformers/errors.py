"""Typed error leaves for the transformers package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import UnimplementedError


@dataclass(kw_only=True)
class TransformerNotImplementedError(UnimplementedError):
    code: ClassVar[str] = "UNIMPLEMENTED_TRANSFORMER_BASE"
    message: str = "Transformer method not implemented"
    operation: str | None = "transform"
