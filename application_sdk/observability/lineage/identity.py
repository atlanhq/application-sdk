"""Canonical ARS identity hash — the single source of truth for the
connector→publish stitch key.

A lineage "miss" can occur at connector-transform time OR at publish-app ARS
resolution time. To attribute a publish-side drop back to the exact connector
edge, both sides must derive the SAME key from the SAME ``arsIdentity.components``
map. This module is that one implementation; the publish-app imports it (it
already depends on application-sdk) so the two sides cannot drift.

Stability rules (each defends a concrete join-break surface):

1. **Fixed field order** — the hash reads named identity fields in a fixed order,
   so the key is independent of ``components`` dict key order.
2. **Case policy** — SQL-identity fields (database/schema/table/column) are
   UPPER-normalized to match the publish resolver's ``UPPER()`` JOIN semantics
   (the ``ARS_EDGE_CASE_MISMATCH`` class); ``connectorType`` is lower-cased.
3. **Null omission** — missing / ``None`` / empty values normalize to the empty
   string deterministically.
4. **Versioned** — every record carries :data:`IDENTITY_SCHEMA_VERSION`; the
   stitch refuses to join across mismatched versions rather than mis-joining.

If a connector's identity needs additional discriminating components, extend
:data:`IDENTITY_FIELDS` here (the single canonical place) and bump
:data:`IDENTITY_SCHEMA_VERSION` — never fork the algorithm per connector.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

#: Bump whenever the hashed field set or normalization changes. The stitch
#: refuses to join records carrying a different version.
IDENTITY_SCHEMA_VERSION = 1

#: ``connectorType`` is lower-cased; all other fields are UPPER-cased to mirror
#: the publish resolver's case-folded JOIN. Order here is the canonical hash order.
IDENTITY_FIELDS = (
    "connectorType",
    "databaseName",
    "schemaName",
    "tableName",
    "columnName",
)

_LOWER_FIELDS = frozenset({"connectorType"})
_SEP = "|"


def _normalize_field(name: str, value: Any) -> str:
    """Normalize a single identity component to its canonical string form."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text.lower() if name in _LOWER_FIELDS else text.upper()


def canonical_identity_string(components: Mapping[str, Any]) -> str:
    """Build the deterministic, case-normalized canonical string for *components*.

    Exposed (alongside :func:`components_hash`) so the publish-side DuckDB pass
    can be validated to produce a byte-identical string before hashing.
    """
    return _SEP.join(_normalize_field(f, components.get(f)) for f in IDENTITY_FIELDS)


def components_hash(components: Mapping[str, Any] | None) -> str:
    """Return the canonical SHA-256 hex digest for an ``arsIdentity.components`` map.

    Deterministic and case/key-order invariant. ``None``/empty maps to the hash
    of the all-empty identity (callers should not pass empty components for a
    real edge, but the function never raises).
    """
    canonical = canonical_identity_string(components or {})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stitch_key(
    qualified_name: str,
    direction: str,
    ordinal: int,
    components: Mapping[str, Any] | None,
) -> str:
    """The full edge-grain join key used by the connector→publish reconcile."""
    return _SEP.join(
        [
            qualified_name or "",
            direction or "",
            str(ordinal),
            components_hash(components),
        ]
    )


__all__ = [
    "IDENTITY_SCHEMA_VERSION",
    "IDENTITY_FIELDS",
    "canonical_identity_string",
    "components_hash",
    "stitch_key",
]
