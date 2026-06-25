"""FailureDetails — Pydantic wire envelope carried in ApplicationError.details."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from application_sdk.errors.categories import Audience, FailureCategory

# Keys that may carry secrets — rejected at envelope construction.
_EVIDENCE_KEY_DENYLIST: frozenset[str] = frozenset(
    {
        "auth_header",
        "authorization",
        "cookie",
        "token",
        "password",
        "secret",
        "api_key",
        "private_key",
    }
)


class FailureDetails(BaseModel):
    """Pydantic envelope serialized into ``ApplicationError.details=[…]``.

    Round-trips through ``pydantic_data_converter`` without any dict adapter.
    Consumers read routing fields (``category``, ``code``, ``retryable``,
    ``audience``) as typed attributes; per-error context lives in ``evidence``,
    whose keys match the dataclass fields of the Error that produced it.

    Field semantics:
    - ``category``: the closed FailureCategory enum — what happened.
    - ``audience``: who needs to act (USER / PLATFORM / APP_OWNER). Closed
      three-value enum; every leaf must pick one. There is no UNKNOWN
      escape hatch — if the locus is unclear the answer is APP_OWNER
      (the team that wrote the code investigates and reclassifies).
    - ``code``: app-owned string for fine-grained identification.
    - ``suggested_action``: optional imperative hint ("regrant Glue read access").
      The voice shifts with the audience: customer-facing text when
      ``audience=USER``, engineer-facing remediation when ``audience=APP_OWNER``,
      runbook hint when ``audience=PLATFORM``.
    - ``evidence``: per-error context whose schema is the producing dataclass.

    Tenant identity is intentionally NOT carried on this envelope. Per-tenant
    attribution is the consumer's job — the producer (the failing app) does
    not know or carry tenant context. The Automation Engine (or any other
    consumer that reads ``ApplicationError.details``) attaches tenant from
    its own context when it ingests the failure.
    """

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

    @field_validator("evidence")
    @classmethod
    def _no_secret_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Exact match covers standalone names; suffix match catches compound variants
        # like ``client_secret`` or ``db_password`` without blocking generic names
        # such as ``object_key`` or ``cache_key``.
        _SUFFIX_DENYLIST = ("_secret", "_password", "_token")
        bad = {
            k
            for k in v
            if k.lower() in _EVIDENCE_KEY_DENYLIST
            or any(k.lower().endswith(s) for s in _SUFFIX_DENYLIST)
        }
        if bad:
            raise ValueError(  # stdlib-interop: pydantic field_validator requires ValueError
                "evidence keys may not use secret-named fields: %s" % sorted(bad)
            )
        return v
