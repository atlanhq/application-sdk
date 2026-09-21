"""Secret redaction for anything bound for the HTTP wire.

Ported from ``application_sdk.errors.base`` / ``.wire`` so the hosted path
redacts what the worker path already redacts. Before this existed the two
disagreed, and the hosted one is the side facing Atlan: a driver exception
carrying a DSN shipped the source password straight into the
``/workflows/v1/check`` response body.

Stdlib only (``re``), so the thin base install is unaffected.

One deliberate divergence from application_sdk: secret-named evidence keys are
MASKED here, not rejected. application_sdk raises a ``ValueError`` from the
envelope validator and pairs it with a degradation path. ``server_sdk`` builds
failure details inside a "report NOT_READY, never 500" boundary
(``handler/sql.py``), so a raising validator would convert a redaction problem
into the 500 that boundary exists to prevent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Userinfo in URLs of any scheme: postgresql://user:pass@host -> postgresql://***@host.
# `(?:[^@\s]+@)+` is greedy to the last `@` so a raw `@` inside the password
# does not leave the tail exposed; over-redacting is the safe direction.
_URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)(?:[^@\s]+@)+", re.IGNORECASE)

# Secret query/DSN params. `pwd` covers ODBC (`UID=sa;PWD=...`). The braced
# alternative is tried first because ODBC quotes values containing the `;`
# separator as `PWD={secret;with;semicolons}` and the bare class would stop at
# that first `;` and leak the tail.
#
# `uid` and a generic `token` are deliberately absent: they are not credentials,
# and they would redact `run_guid=`, `next_token=` and the pagination cursors an
# on-call needs. `signature`/`sig` cover presigned object-store URLs, where the
# signature -- not the credential -- is what actually authorises the request.
_SECRET_PARAM_RE = re.compile(
    r"(?i)((?:api_key|access_token|auth_token|password|passwd|pwd|secret|credential"
    r"|private_key|signature|(?<![a-z0-9_])sig)=)(?:\{[^}]*\}|[^\s&,;#]+)",
)

#: Recursion bound for :func:`redact_wire_value`, so a pathologically deep
#: structure truncates rather than overflowing the stack.
_REDACT_MAX_DEPTH = 32

# obstore appends a multi-line Rust `Debug` dump after the provider's response
# body, so a keep-the-tail truncation would preserve the dump and drop the
# diagnostic. Strip it before capping (FND-957).
_DEBUG_SOURCE_TAIL_RE = re.compile(r"\n+Debug source:\n.*\Z", re.DOTALL)

# Sized so a full provider error response survives. Truncation keeps BOTH ends:
# a backend error puts the request URL at the head and the reason at the tail.
_CAUSE_MAX_LEN = 2000
_CAUSE_HEAD_LEN = 1200
_CAUSE_TAIL_LEN = 700

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


def redact_secrets(text: str) -> str:
    """Redact URL userinfo and known secret params in one string."""
    return _SECRET_PARAM_RE.sub(r"\1***", _URL_USERINFO_RE.sub(r"\1***@", text))


def redact_wire_value(value: Any, seen: set[int] | None = None, depth: int = 0) -> Any:
    """Redact every string reachable inside a value bound for the wire.

    Non-strings are left alone. A revisited container yields ``None`` and
    anything past :data:`_REDACT_MAX_DEPTH` yields ``"…"``, so a self-
    referential or hostile structure truncates rather than hangs.
    """
    if isinstance(value, str):
        return redact_secrets(value)

    if isinstance(value, dict):
        seen = set() if seen is None else seen
        if id(value) in seen:
            return None
        if depth >= _REDACT_MAX_DEPTH:
            return "…"
        seen.add(id(value))
        try:
            return {k: redact_wire_value(v, seen, depth + 1) for k, v in value.items()}
        finally:
            seen.discard(id(value))

    if isinstance(value, (list, tuple, set, frozenset)):
        seen = set() if seen is None else seen
        if id(value) in seen:
            return None
        if depth >= _REDACT_MAX_DEPTH:
            return "…"
        seen.add(id(value))
        try:
            redacted = [redact_wire_value(v, seen, depth + 1) for v in value]
        finally:
            seen.discard(id(value))
        if isinstance(value, tuple) and hasattr(type(value), "_fields"):
            # A NamedTuple takes positional fields, not an iterable.
            try:
                return type(value)(*redacted)
            except Exception:  # noqa: BLE001 - connector-authored type
                return tuple(redacted)
        try:
            return type(value)(redacted)
        except Exception:  # noqa: BLE001 - a container subclass whose
            # constructor raises must not crash the degrade path.
            return tuple(redacted) if isinstance(value, tuple) else redacted

    return value


def secret_named_evidence_keys(evidence: Mapping[str, Any]) -> frozenset[str]:
    """The ``evidence`` keys whose *name* marks them as secret-bearing.

    Example:
        >>> sorted(secret_named_evidence_keys({"host": "db", "api_key": "x"}))
        ['api_key']
    """
    return frozenset(
        k
        for k in evidence
        if k.lower() in _EVIDENCE_KEY_DENYLIST
        or any(k.lower().endswith(s) for s in _EVIDENCE_KEY_SUFFIX_DENYLIST)
    )


def mask_secret_named_keys(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Replace secret-named values with ``***``, keeping the key.

    Keeping the key preserves "a password was involved" for whoever reads the
    envelope, which dropping it silently would not.
    """
    bad = secret_named_evidence_keys(evidence)
    if not bad:
        return dict(evidence)
    return {k: (_MASK if k in bad else v) for k, v in evidence.items()}


def sanitize_cause_repr(exc: BaseException) -> str:
    """A length-capped, secret-redacted string for a cause exception.

    Redaction runs before truncation, so keeping a tail can never expose an
    unredacted secret (FND-957).
    """
    text = _DEBUG_SOURCE_TAIL_RE.sub("", redact_secrets(str(exc)))
    if len(text) > _CAUSE_MAX_LEN:
        elided = len(text) - _CAUSE_HEAD_LEN - _CAUSE_TAIL_LEN
        text = (
            text[:_CAUSE_HEAD_LEN]
            + f"…[{elided} chars elided]…"
            + text[-_CAUSE_TAIL_LEN:]
        )
    return f"{type(exc).__name__}: {text}"
