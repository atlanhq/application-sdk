"""Decoder for a credential's ``extra`` field.

Lives apart from :mod:`application_sdk.credentials.utils` (which reaches the
object store and the worker constants) so the handler contracts can use it on
an api-only install. ``credentials.utils`` re-exports it.
"""

from __future__ import annotations

import json
from typing import Any

import orjson

from application_sdk._logging import get_logger

logger = get_logger(__name__)


def parse_credentials_extra(
    credentials: dict[str, Any], *, strict: bool = True
) -> dict[str, Any]:
    """Decode the ``extra`` field of a credential dict.

    ``extra`` is stored in two legal shapes — a nested object, or that same
    object serialized to a JSON string — because its producers (the Atlan UI,
    Heracles, Argo templates, agent JSON) straddle the v2/v3 credential
    contract boundary. A reader that handles only one shape silently drops
    whatever the other shape carried, so the credential-resolution and
    gate-flattening paths share this decoder rather than shape-matching
    locally: ``clients/sql.py`` and
    :func:`~application_sdk.handler.contracts.flatten_credentials_to_pairs`.

    It is **not** yet the only reader of ``extra`` in the SDK. These still
    parse it independently and remain to be routed through here:

    * ``storage/cloud.py`` — decodes both shapes, but with its own error type
      and an additional ``extras`` alias key.
    * ``credentials/agent.py`` (secret-reference collection and substitution)
      and ``infrastructure/_dapr/credential_vault.py`` (secret substitution)
      — ``isinstance(extra, dict)`` only, so a JSON-string ``extra`` is
      skipped rather than decoded.

    Always returns a mapping. Absent, null, and empty ``extra`` are all "no
    extra" — handing back the raw ``None`` instead only moved the failure to
    an ``AttributeError`` on the caller's next ``.get()``.

    Args:
        credentials: Credential dict that may carry an ``extra`` field.
        strict: Policy for an ``extra`` that is present but unusable (not
            decodable, or not a JSON object). ``True`` — the runtime-client
            policy — raises: a connector cannot build a DSN without it, and
            a typed credential error beats a downstream ``AttributeError``.
            ``False`` — the flattening policy — returns ``{}`` instead, for
            callers that run where no one is positioned to distinguish a
            malformed credential from an absent one and so must never raise.

    Returns:
        The decoded ``extra`` object, or ``{}`` when it is absent (or
        unusable and ``strict`` is ``False``).

    Raises:
        CredentialParseError: ``strict`` is set and ``extra`` is present but
            is neither valid JSON nor a JSON object.
    """
    extra: Any = credentials.get("extra")

    # ``isinstance`` before the emptiness test: ``extra`` is arbitrarily typed
    # here, and a bare ``== ""`` on a container that overloads equality returns
    # a container rather than a bool, raising on the truthiness check.
    if extra is None or (isinstance(extra, str) and not extra):
        return {}

    if isinstance(extra, dict):
        return extra

    def _reject(message: str, cause: Exception | None = None) -> dict[str, Any]:
        if not strict:
            # Never silent: dropping ``extra`` costs the caller every
            # connection param stored inside it, and a lenient caller by
            # definition has no error to surface. The log line is the only
            # trace, so it must carry the reason — but never the value, which
            # is credential material.
            logger.warning(
                "Dropping unusable credentials extra field, continuing without "
                "it (any connection params stored inside it will be absent): %s",
                message,
                exc_info=cause is not None,
            )
            return {}
        from application_sdk.credentials.errors import (  # noqa: PLC0415
            CredentialParseError,
        )

        raise CredentialParseError(
            message=message, credential_name="extra", cause=cause
        ) from cause

    if isinstance(extra, str):
        try:
            extra = orjson.loads(extra)
        except json.JSONDecodeError as e:
            # conformance: ignore[E007] not swallowed — _reject raises under strict and logs a WARNING with exc_info otherwise; the rule is lexical and cannot follow into the helper
            return _reject("Invalid JSON in credentials extra field", e)

    if not isinstance(extra, dict):
        return _reject(
            "Credentials extra field is not a JSON object "
            f"(decoded to {type(extra).__name__})"
        )

    return extra
