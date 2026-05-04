"""FailureDetails — Pydantic wire envelope carried in ApplicationError.details."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from application_sdk.errors.categories import FailureCategory


class FailureDetails(BaseModel):
    """Pydantic envelope serialized into ``ApplicationError.details=[…]``.

    Round-trips through ``pydantic_data_converter`` without any dict adapter.
    Consumers read routing fields (``category``, ``code``, ``retryable``) as
    typed attributes; per-error context lives in ``evidence``, whose keys match
    the dataclass fields of the Error that produced it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: FailureCategory
    code: str
    retryable: bool
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    app_name: str | None = None
    run_id: str | None = None
    cause_repr: str | None = None
