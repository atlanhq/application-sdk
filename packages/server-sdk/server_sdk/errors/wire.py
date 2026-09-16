"""Typed failure envelope — the model ``PreflightCheck.error`` holds.

Frozen + ``extra="forbid"`` so it validates strictly. Only ``message`` and
``suggested_action`` are load-bearing for the preflight message-resolution
rule, but the full field set is carried on the wire when a failed check
serializes its typed error.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from server_sdk.errors.categories import Audience, FailureCategory


class FailureDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: FailureCategory
    code: str
    retryable: bool
    audience: Audience = Audience.APP_OWNER
    message: str
    suggested_action: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    app_name: str | None = None
    run_id: str | None = None
    cause_repr: str | None = None
