"""Canonical hash of a ref's ``arsIdentity.components``: the cross-app join key.

The app that emits a ref and the lookup that resolves it both hash the same
``components`` map with :func:`components_hash`, so a lookup outcome can be
joined back to the asset that emitted the ref.

Version 2 adds the leaf name. A table ref names its table in ``name`` and a
column ref names its column in ``name`` (with ``tableName``); version 1 hashed
``columnName`` only, so every table in a schema hashed the same. ``name`` falls
back to ``columnName``, mirroring the publish lookup's
``COALESCE(name, columnName)``, so a producer that still sets ``columnName``
hashes the same as one that sets ``name``.

Rules: fixed field order (independent of dict order); ``connectorType``
lower-cased, the rest upper-cased to match the lookup's ``UPPER()`` join;
``None``, blank and whitespace-only values become ``""``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

#: Bump whenever the hashed fields or the normalisation change. Records with
#: different versions must never be joined.
IDENTITY_SCHEMA_VERSION = 2

IDENTITY_FIELDS = ("connectorType", "databaseName", "schemaName", "tableName", "name")

_SEP = "|"


def _normalize(field: str, value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return text.lower() if field == "connectorType" else text.upper()


def canonical_identity_string(components: Mapping[str, Any]) -> str:
    values = dict(components)
    # Null only, as SQL COALESCE does: a blank ``name`` does not fall back.
    if values.get("name") is None:
        values["name"] = values.get("columnName")
    return _SEP.join(_normalize(f, values.get(f)) for f in IDENTITY_FIELDS)


def components_hash(components: Mapping[str, Any] | None) -> str:
    """SHA-256 hex of the canonical identity string."""
    canonical = canonical_identity_string(components or {})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
