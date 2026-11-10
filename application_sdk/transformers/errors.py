"""Typed error leaves for the deprecated transformers package.

.. deprecated:: 3.56.0
    Removed in v4.0 with the rest of ``application_sdk.transformers``. Catch
    ``application_sdk.errors.UnimplementedError`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from typing_extensions import deprecated

from application_sdk.errors.leaves import UnimplementedError


@deprecated(
    "application_sdk.transformers.errors.TransformerNotImplementedError is "
    "deprecated; catch application_sdk.errors.UnimplementedError (its base) "
    "instead — will be removed in v4.0."
)
@dataclass(kw_only=True)
class TransformerNotImplementedError(UnimplementedError):
    code: ClassVar[str] = "UNIMPLEMENTED_TRANSFORMER_BASE"
    message: str = "Transformer method not implemented"
    operation: str | None = "transform"
