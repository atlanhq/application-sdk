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


def parse_credentials_extra(credentials: dict[str, Any]) -> dict[str, Any]:
    """Return ``credentials['extra']`` as a dict (parsing a JSON string form)."""
    extra = credentials.get("extra", {})
    if isinstance(extra, str):
        try:
            extra = json.loads(extra) if extra.strip() else {}
        except json.JSONDecodeError:
            extra = {}
    return extra if isinstance(extra, dict) else {}


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
