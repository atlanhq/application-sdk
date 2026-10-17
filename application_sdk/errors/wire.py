"""FailureDetails — Pydantic wire envelope carried in ApplicationError.details."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from application_sdk.errors.base import redact_secrets, redact_wire_value
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

# Compound variants like ``client_secret`` or ``db_password``, matched by
# suffix so generic names such as ``object_key`` or ``cache_key`` still pass.
_EVIDENCE_KEY_SUFFIX_DENYLIST: tuple[str, ...] = ("_secret", "_password", "_token")


def secret_named_evidence_keys(evidence: Mapping[str, Any]) -> frozenset[str]:
    """The ``evidence`` keys whose *name* marks them as secret-bearing.

    :class:`FailureDetails` rejects these at the top level and masks them
    (``***``) below it. Exposed so a producer holding a rejected verdict can strip
    exactly the keys the envelope refuses, without re-deriving the denylist.

    Example:
        >>> sorted(secret_named_evidence_keys({"host": "db", "api_key": "x"}))
        ['api_key']
    """
    # isinstance guard: pydantic validates only the top-level key type, so a
    # nested mapping reaches here with any hashable key. A non-str key cannot be
    # a secret-NAMED key; its value is still redacted by redact_wire_value.
    return frozenset(
        k
        for k in evidence
        if isinstance(k, str)
        and (
            k.lower() in _EVIDENCE_KEY_DENYLIST
            or any(k.lower().endswith(s) for s in _EVIDENCE_KEY_SUFFIX_DENYLIST)
        )
    )


_MASK = "***"
_MASK_MAX_DEPTH = 32


def mask_secret_named_keys(
    evidence: Mapping[str, Any], _depth: int = 0
) -> dict[str, Any]:
    """Replace secret-named values with ``***`` at every depth, keeping the key."""
    if _depth >= _MASK_MAX_DEPTH:
        return {}
    bad = secret_named_evidence_keys(evidence)
    return {
        key: (_MASK if key in bad else _mask_nested(value, _depth + 1))
        for key, value in evidence.items()
    }


def _mask_nested(value: Any, depth: int) -> Any:
    if depth >= _MASK_MAX_DEPTH:
        return "…"
    if isinstance(value, Mapping):
        return mask_secret_named_keys(value, depth)
    if isinstance(value, (list, tuple)):
        masked = [_mask_nested(v, depth + 1) for v in value]
        if isinstance(value, tuple):
            if hasattr(type(value), "_fields"):
                try:
                    return type(value)(*masked)
                except Exception:  # noqa: BLE001 — connector-authored NamedTuple
                    return tuple(masked)
            return tuple(masked)
        return masked
    return value


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

    @field_validator("message", "suggested_action")
    @classmethod
    def _redact_free_text(cls, v: str | None) -> str | None:
        """Scrub credentials out of the handler-authored strings, once, here.

        These two fields are the only free text on the envelope and both are
        written by the app, so a driver's connection string or a presigned URL
        lands in them routinely. This model is what reaches Temporal history,
        the Automation Engine and every log row, so redacting where it is built
        covers every consumer at once — including the ones not written yet.
        Idempotent, so an envelope replayed off the wire is unchanged.
        ``evidence`` is handled below: secret-named keys masked, values redacted.
        """
        return v if v is None else redact_secrets(v)

    @field_validator("evidence")
    @classmethod
    def _scrub_evidence(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Reject secret-named top-level keys; mask nested ones; redact every value.

        A secret-named *top-level* key is a producer bug and is rejected, as it
        always was — ``sql_app``'s degrade ladder strips it and keeps the typed
        routing. Two gaps are closed here, because this envelope is what reaches
        Temporal history, the Automation Engine, every log row and the HTTP body:

        * a secret-named key one level down (``{"config": {"password": ...}}``)
          passed the top-level check untouched — it is masked (``***``);
        * evidence *values* were never redacted, so a leaf field such as
          ``endpoint`` carrying a DSN shipped its password — every string is now
          scrubbed like the free-text fields.

        Idempotent, so an envelope replayed off the wire is unchanged.
        """
        bad = secret_named_evidence_keys(v)
        if bad:
            raise ValueError(  # stdlib-interop: pydantic field_validator requires ValueError
                "evidence keys may not use secret-named fields: %s" % sorted(bad)
            )
        return redact_wire_value({k: _mask_nested(val, 1) for k, val in v.items()})
