"""Evidence-key masking for the HTTP wire.

String and value redaction (``redact_secrets``, ``redact_wire_value``,
``sanitize_cause_repr``) is the SDK's own, in :mod:`application_sdk_api.errors.base`,
and is re-exported here so existing callers keep working.

What this module adds is masking *by key name*: secret-named evidence keys are
replaced with ``***`` rather than rejected. ``FailureDetails`` rejects them in its
validator; the serving boundary builds failure details inside a "report
NOT_READY, never 500" path, where a raising validator would turn a redaction
problem into the 500 that boundary exists to prevent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The string/value redactors are the SDK's own; this module adds only the
# evidence-key masking the HTTP surface applies on top.
from application_sdk_api.errors.base import (  # noqa: F401
    redact_secrets,
    redact_wire_value,
    sanitize_cause_repr,
)

#: Recursion bound for :func:`redact_wire_value`, so a pathologically deep
#: structure truncates rather than overflowing the stack.
_REDACT_MAX_DEPTH = 32

# Evidence keys that may carry secrets.
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

# Compound variants (`client_secret`, `db_password`), matched by suffix so
# generic names like `object_key` still pass.
_EVIDENCE_KEY_SUFFIX_DENYLIST: tuple[str, ...] = ("_secret", "_password", "_token")

_MASK = "***"


def secret_named_evidence_keys(evidence: Mapping[str, Any]) -> frozenset[str]:
    """The ``evidence`` keys whose *name* marks them as secret-bearing.

    Example:
        >>> sorted(secret_named_evidence_keys({"host": "db", "api_key": "x"}))
        ['api_key']
    """
    # isinstance guard, not str(k): pydantic validates only the TOP-level key
    # type, so a nested mapping reaches here with any hashable key and a bare
    # k.lower() raised AttributeError straight out of a frozen-model validator
    # -- the 500 that this module's mask-instead-of-reject divergence exists to
    # prevent. Skipping is right rather than coercing: a non-str key cannot be
    # a secret-NAMED key, and str(b"password") is "b'password'", which matches
    # neither denylist anyway. Its VALUE still goes through redact_wire_value.
    return frozenset(
        k
        for k in evidence
        if isinstance(k, str)
        and (
            k.lower() in _EVIDENCE_KEY_DENYLIST
            or any(k.lower().endswith(s) for s in _EVIDENCE_KEY_SUFFIX_DENYLIST)
        )
    )


def mask_secret_named_keys(
    evidence: Mapping[str, Any], _depth: int = 0
) -> dict[str, Any]:
    """Replace secret-named values with ``***``, keeping the key.

    Keeping the key preserves "a password was involved" for whoever reads the
    envelope, which dropping it silently would not.

    Recurses into nested mappings and sequences. The value redaction already
    walked the whole structure, so masking only the top level left a
    secret-NAMED key one level down untouched -- and evidence is routinely
    nested (a connector attaching its resolved config, say). Bounded by the
    same depth limit, so a hostile structure truncates rather than hangs.
    """
    if _depth >= _REDACT_MAX_DEPTH:
        return {}
    bad = secret_named_evidence_keys(evidence)
    out: dict[str, Any] = {}
    for key, value in evidence.items():
        if key in bad:
            out[key] = _MASK
        else:
            out[key] = _mask_nested(value, _depth + 1)
    return out


def _mask_nested(value: Any, depth: int) -> Any:
    """Apply :func:`mask_secret_named_keys` to any mapping inside ``value``."""
    if depth >= _REDACT_MAX_DEPTH:
        return "…"
    if isinstance(value, Mapping):
        return mask_secret_named_keys(value, depth)
    if isinstance(value, (list, tuple)):
        masked = [_mask_nested(v, depth + 1) for v in value]
        if isinstance(value, tuple):
            if hasattr(type(value), "_fields"):
                # Mirror redact_wire_value: a NamedTuple takes positional
                # fields. Flattening here undid the preservation one function
                # above, so in-process readers of .evidence lost the names.
                try:
                    return type(value)(*masked)
                except Exception:  # noqa: BLE001 - connector-authored type
                    return tuple(masked)
            return tuple(masked)
        return type(value)(masked)
    return value
