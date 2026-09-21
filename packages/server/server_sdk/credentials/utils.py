"""Credential helpers used on the serving path.

- ``parse_credentials_extra`` returns the ``extra`` sub-object as a dict
  (JSON-decoding it if it arrived as a string).
- ``credentials_list_to_dict`` reassembles the wire ``[{key, value}]`` list
  (as carried on ``AuthInput.credentials``) into the flat dict a SQL client's
  ``load()`` expects, hoisting ``extra.<k>`` pairs back under ``extra``.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from server_sdk.errors.leaves import InvalidInputError
from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def parse_credentials_extra(
    credentials: dict[str, Any], *, strict: bool = True
) -> dict[str, Any]:
    """Return ``credentials['extra']`` as a dict (parsing a JSON string form).

    ``extra`` arrives in two legal shapes -- a nested object, or that same
    object serialized to a JSON string -- because its producers straddle the
    v2/v3 credential contract boundary.

    Args:
        credentials: credential dict that may carry an ``extra`` field.
        strict: policy for an ``extra`` that is present but unusable. ``True``
            (the runtime-client policy) raises: a connector cannot build a DSN
            without it, and a typed credential error beats losing every
            connection param stored inside it. ``False`` returns ``{}`` and
            logs, for callers that must never raise.

    Raises:
        InvalidInputError: ``strict`` and ``extra`` is present but is neither
            valid JSON nor a JSON object.
    """
    extra: Any = credentials.get("extra")

    # isinstance before the emptiness test: `extra` is arbitrarily typed here,
    # and a bare truthiness check on a container overloading __eq__/__bool__
    # can raise.
    if extra is None or (isinstance(extra, str) and not extra.strip()):
        return {}
    if isinstance(extra, dict):
        return extra

    def _reject(message: str, cause: Exception | None = None) -> dict[str, Any]:
        if not strict:
            # Never silent: dropping `extra` costs the caller every connection
            # param inside it. The log carries the reason, never the value.
            logger.warning(
                "Dropping unusable credentials extra field, continuing without it "
                "(any connection params stored inside it will be absent): %s",
                message,
                exc_info=cause is not None,
            )
            return {}
        raise InvalidInputError(
            message=message, field="extra", constraint="json_object"
        ) from cause

    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError as exc:
            return _reject("Invalid JSON in credentials extra field", exc)

    if not isinstance(extra, dict):
        return _reject(
            f"Credentials extra field decoded to {type(extra).__name__}, not an object"
        )
    return extra


def _coerce(value: str) -> Any:
    """Best-effort decode a wire string back to its JSON value if it looks like one."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped[:1] in "{[" or stripped in ("true", "false", "null"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


def credentials_list_to_dict(
    creds: Iterable[Any],
) -> dict[str, Any]:
    """Turn ``[{key, value}]`` (dicts or ``HandlerCredential``) into a flat dict.

    ``extra.<k>`` keys are nested back under an ``extra`` dict.
    """
    out: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for item in creds or []:
        if isinstance(item, dict):
            key, value = item.get("key", ""), item.get("value", "")
        else:  # HandlerCredential
            key, value = getattr(item, "key", ""), getattr(item, "value", "")
        if not key:
            continue
        if key.startswith("extra."):
            extra[key[len("extra.") :]] = _coerce(value)
        else:
            out[key] = _coerce(value)
    if extra:
        # A top-level "extra" pair can already hold a dict (a JSON object that
        # _coerce parsed); merge into it. If it's present but not a dict
        # (malformed), the hoisted extra.<k> pairs win rather than raising.
        existing = out.get("extra")
        out["extra"] = {**existing, **extra} if isinstance(existing, dict) else extra
    return out
