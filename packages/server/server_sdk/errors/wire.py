"""Typed failure envelope — the model ``PreflightCheck.error`` holds.

Frozen + ``extra="forbid"`` so it validates strictly. Only ``message`` and
``suggested_action`` are load-bearing for the preflight message-resolution
rule, but the full field set is carried on the wire when a failed check
serializes its typed error.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from server_sdk.errors.categories import Audience, FailureCategory
from server_sdk.errors.redaction import (
    mask_secret_named_keys,
    redact_secrets,
    redact_wire_value,
)


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

    # Scrub at the envelope, not at the producer: _summarize_check serialises
    # this whole model into the /check response body, and a driver exception
    # carrying a DSN would otherwise ship the source password to the caller.
    # Validating here covers every construction path, including a connector
    # building FailureDetails directly.
    @field_validator("evidence")
    @classmethod
    def _scrub_evidence(cls, v: dict[str, Any]) -> dict[str, Any]:
        return redact_wire_value(v)

    @field_validator("message", "cause_repr", "suggested_action")
    @classmethod
    def _scrub_text(cls, v: str | None) -> str | None:
        # message is the likeliest carrier: the documented default for a SQL
        # connector is `message=str(exc)`, and a driver's str() embeds the DSN.
        return redact_secrets(v) if isinstance(v, str) else v
