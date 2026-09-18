"""The entity *envelope* — what a transformed JSONL line looks like (FND-2137).

:mod:`application_sdk.common.asset_serialization` closed the **dispatch** seam
(FND-2056): it decides how to turn a mapper's return value into Atlas wire
bytes, and raises instead of silently publishing raw source rows. It did not
own the envelope — the shape of the line once serialisation has happened — so
every connector hand-rolled its own post-serialisation pass over the result.

A scan of the six SqlApp-pattern connectors found four mutually incompatible
envelope strategies, two of which disagreed about *where relationship
references live*. Two apps publishing to the same downstream disagreed about
the wire contract and nothing in the SDK could tell them apart. This module is
where that is decided once.

Why flattened is the default
----------------------------

``atlan-publish-app`` does set-based append/remove diffing for ``inputs``,
``outputs`` and ``upstreamTables``, and it reads them out of ``attributes``.
A top-level ``relationshipAttributes`` key falls through to its catch-all
branch: whole-dict equality, forwarded verbatim. So under the pyatlan-native
envelope the relationship append/remove path never fires and the whole dict is
replaced on every publish. Connectors that emit no lineage get away with it;
the first one that does would lose incremental relationship diffing silently.

Corroborating, in this repo: :mod:`application_sdk.validation.assets` had to
widen to ``from_atlas_json`` because connector transformed output puts
relationships in ``attributes`` — with the strict decoder, ~98% of assets read
as invalid. And the SDK's own reference producer of transformed output,
``application_sdk.testing.harness.seed``, already writes the flattened shape.

:attr:`EnvelopeShape.PYATLAN` therefore exists only as a migration lever for a
connector whose *released* output is the other shape, and is removed in 4.0.

The flattening is pyatlan's, not ours
-------------------------------------

``pyatlan_v9.model.transform.to_atlas_format`` already emits exactly this
envelope from the flat Struct: relationship fields land in ``attributes``, and
``relationshipAttributes`` / ``appendRelationshipAttributes`` /
``removeRelationshipAttributes`` are never emitted at all. It is also *cheaper*
than the nested encoder the SDK uses today — ``to_nested_bytes`` builds a whole
``*Nested`` Struct plus a ``categorize_relationships`` pass, where
``to_atlas_format`` is one msgspec encode and a key partition. Measured on one
``Column``, 5000 iterations::

    to_nested_bytes                :  11.0 us/call
    to_atlas_format + orjson.dumps :   3.0 us/call

So the v9 path delegates, and :func:`flatten_envelope` exists only for the
shapes ``to_atlas_format`` cannot take: plain dicts and pyatlan v1 models.

The two paths agree on the question this module exists to settle — where
relationship refs live — and, from pyatlan 11.3.0, on null handling too: both
leave an explicit ``None`` exactly where the mapper wrote it, and both omit a
field that was never set.

That second agreement is a pinned dependency premise, not a property of this
code. Up to pyatlan 11.2.0 ``to_atlas_format`` dropped explicit nulls in its
key partition, so the flattened envelope disagreed both with
:func:`flatten_envelope` and with ``to_nested_bytes`` — which is why
``pyproject.toml`` floors at ``pyatlan>=11.3`` rather than the ``>=11`` range
that would otherwise do. ``TestPyatlanFlattenContract`` in
``tests/unit/common/test_entity_envelope.py`` is what catches a move here.
See :func:`flatten_envelope` for why the SDK never mirrored the drop on its
own side.

Public API::

    from application_sdk.common.entity_envelope import (
        EntityDecorations,
        EntityEnvelopePolicy,
        EnvelopeShape,
    )

    class TeradataApp(SqlApp):
        entity_envelope = EntityEnvelopePolicy(sql_dialect="teradata")
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "DEFAULT_ENVELOPE",
    "EntityDecorations",
    "EntityEnvelopePolicy",
    "EnvelopeShape",
    "apply_envelope",
    "flatten_envelope",
    "to_atlas_format_dict",
]

#: Attribute keys that carry a SQL asset's DDL. ``Table`` declares
#: ``tableDefinition``; ``View`` and ``MaterialisedView`` declare
#: ``definition``. They are different attributes on different types, not two
#: spellings of one — see FND-2148, where treating them as synonyms wrote a
#: table's DDL to an attribute ``Table`` does not declare. Both are listed
#: because :attr:`EntityEnvelopePolicy.sql_dialect` is asking "does this asset
#: carry DDL", which either key answers.
_DEFINITION_KEYS: Final[tuple[str, ...]] = ("tableDefinition", "definition")


class EnvelopeShape(StrEnum):
    """Where relationship references live in the serialised entity."""

    FLATTENED = "flattened"
    """Relationship refs merged into ``attributes`` — the default.

    What ``atlan-publish-app``'s diff engine reads, what the SDK's own seed
    harness writes, and what ``application_sdk.validation.assets`` decodes.
    For a ``pyatlan_v9`` asset this is ``to_atlas_format`` unmodified.
    """

    PYATLAN = "pyatlan"
    """Refs stay under a top-level ``relationshipAttributes`` key.

    .. deprecated:: 3.36.0
        A migration lever, not a supported second contract. It exists so a
        connector whose *released* output is this shape can pin it for one
        cycle rather than flipping its wire format — and its publish diff
        cache — on an SDK bump. Removed in v4.0.

    No runtime ``DeprecationWarning`` is raised: the only place this value is
    read is per-record inside the transform loop, and a warning there would
    fire once per asset.
    """


@dataclass(frozen=True)
class EntityEnvelopePolicy:
    """How one connector's serialised entities are shaped.

    Declared once per app as a class attribute rather than decided per mapper:
    the envelope is a property of the *downstream contract*, not of any one
    entity type, and per-mapper choice is how the fleet ended up with four of
    them.

    Attributes:
        shape: Where relationship refs live. See :class:`EnvelopeShape`.
        sql_dialect: Stamped as ``attributes.sqlDialect`` on assets that carry
            DDL, so downstream SQL parsing (Query Intelligence) knows which
            grammar to read a view definition with. The *value* is static per
            connector; the *condition* is per record — only definition-bearing
            assets get it. ``None`` (the default) stamps nothing.

            Here rather than on the mapper because no ``pyatlan_v9`` asset
            type declares ``sqlDialect`` — an asset-returning mapper has
            nowhere to put it and cannot set it however it tries. That makes
            it the SDK's to inject, in the same sense as ``connectionName``.
            A dict-returning mapper *can* set it, and a value it set wins.
    """

    shape: EnvelopeShape = EnvelopeShape.FLATTENED
    sql_dialect: str | None = None

    @property
    def is_identity(self) -> bool:
        """True when this policy asks for nothing the fast byte path can't do.

        ``PYATLAN`` with no dialect is exactly what ``entity_bytes`` did before
        this module existed, so a policy matching it can keep using
        ``to_nested_bytes()``'s raw bytes and skip the JSON round-trip
        entirely. That is what makes the migration lever byte-identical to the
        output it is standing in for, rather than merely equivalent.
        """
        return self.shape is EnvelopeShape.PYATLAN and self.sql_dialect is None


#: The policy applied when a caller passes none. Flattened, no dialect.
DEFAULT_ENVELOPE: Final[EntityEnvelopePolicy] = EntityEnvelopePolicy()


@dataclass(frozen=True)
class EntityDecorations:
    """Top-level entity fields that no pyatlan model field can hold.

    These are read off the entity *root*, beside ``typeName`` — not out of
    ``attributes`` — by apps downstream of the transformed artifact. pyatlan
    has no field for them and its nested wire structs are closed msgspec
    types, so there is nowhere to put them before serialisation. Before this
    class each connector reached past ``entity_bytes`` and mutated the
    serialised dict.

    Typed rather than a ``Mapping[str, Any]`` on purpose. Every field here is a
    named cross-app contract with a specific reader, and ``atlan-publish-app``
    strips unknown root keys before hashing, so a decoration never reaches
    Atlas — it reaches its reader off the artifact or not at all. A free dict
    is how a second undocumented side-channel gets added without anyone
    noticing; adding a field here forces the conversation about who reads it.

    Attributes:
        default_catalog_name: Emitted as ``defaultCatalogName``. Read by Query
            Intelligence into each ``success.json`` row, which lineage-app then
            uses to resolve a bare table/view name to a fully-qualified Atlas
            path.
        default_schema_name: Emitted as ``defaultSchemaName``. Same reader,
            same purpose.
    """

    default_catalog_name: str | None = None
    default_schema_name: str | None = None

    def as_root_fields(self) -> dict[str, str]:
        """The decorations as wire-shaped root keys, omitting unset ones."""
        fields: dict[str, str] = {}
        if self.default_catalog_name is not None:
            fields["defaultCatalogName"] = self.default_catalog_name
        if self.default_schema_name is not None:
            fields["defaultSchemaName"] = self.default_schema_name
        return fields


def flatten_envelope(entity: dict[str, Any]) -> dict[str, Any]:
    """Merge a nested-format entity's relationship refs into ``attributes``.

    The generic path, for the shapes ``to_atlas_format`` cannot take: a plain
    dict from a dict-returning mapper, or a pyatlan v1 model's ``model_dump``.

    **This moves relationship refs and nothing else.** In particular it does
    not drop nulls. An earlier revision did, copying what ``to_atlas_format``
    did at the time, on the reasoning that ``FLATTENED`` should mean one thing
    whatever the mapper returned — but that silently deleted values a
    dict-returning mapper had emitted on purpose. ``atlan-clickhouse-app``'s
    ``_rel()`` emits a null relationship stub to mirror "the legacy
    transformer's all-None-leaves collapse", and its
    ``_positive_bigint_or_none`` emits null ``rowCount`` / ``sizeBytes`` for
    the same v2 parity. Dropping those rehashes every entity in
    ``atlan-publish-app``'s diff cache for no behavioural gain.

    So null handling stays the mapper's decision here, and ``FLATTENED`` means
    exactly "relationship refs live in ``attributes``" — the only question the
    envelope needed to settle.

    pyatlan 11.3.0 reached the same conclusion on its side, which is why the
    two paths now agree rather than the SDK tolerating a known loss. Worth
    keeping the shape of that loss in mind, because the ``>=11.3`` floor in
    ``pyproject.toml`` is all that holds it off. ``pyatlan_v9`` fields are
    three-state (``Union[str, None, UnsetType] = UNSET``), so an asset can
    express an explicit null; up to 11.2.0 ``to_atlas_format`` rendered that
    ``None`` identically to ``UNSET``, as an absent key. It survived review
    because ``atlan-publish-app``'s ``calculate_attributes_diff``
    re-synthesises the clear, emitting ``{key: None}`` when a key present in
    the cached entity is absent from the new one — so a dropped null still
    reached Atlas on the incremental path, and on a create there was nothing
    to clear. That argument no longer has to be made; a producer-side null on
    a v9 asset now reaches the wire as written.

    ``appendRelationshipAttributes`` and ``removeRelationshipAttributes`` are
    dropped rather than merged. That is load-bearing, not tidying:
    ``atlan-publish-app`` *generates* both keys itself from the diff it
    computes, and a producer-side value collides with the diff engine's own.

    Args:
        entity: A nested-format entity dict. Mutated in place and returned.

    Returns:
        *entity*, flattened.
    """
    relationships = entity.pop("relationshipAttributes", None)
    entity.pop("appendRelationshipAttributes", None)
    entity.pop("removeRelationshipAttributes", None)

    if isinstance(relationships, dict):
        # A ref the mapper also set directly in ``attributes`` loses to the
        # relationship field. ``to_atlas_format`` resolves the same collision
        # the same way — the relationship field is the typed one, and the
        # string in ``attributes`` is the hand-written duplicate.
        #
        # A ``None``-valued ref is skipped rather than moved: it carries no
        # reference, and writing it into ``attributes`` would *create* a null
        # the mapper never put there.
        moved = {k: v for k, v in relationships.items() if v is not None}
        attributes = entity.get("attributes")
        if isinstance(attributes, dict):
            attributes.update(moved)
        elif moved:
            entity["attributes"] = moved

    return entity


def to_atlas_format_dict(asset: object) -> dict[str, Any] | None:
    """``pyatlan_v9.to_atlas_format(asset)``, or ``None`` if it doesn't apply.

    Returns ``None`` — rather than raising — when *asset* is not a
    ``pyatlan_v9`` asset or the package is not installed, so the caller falls
    through to :func:`flatten_envelope`. ``pyatlan_v9`` is an optional
    dependency here, as it is everywhere else in this package.

    The import is function-level for the same reason as the rest of the repo:
    it keeps ``pyatlan_v9`` off the import path of an app that never
    transforms assets. After the first call it is a ``sys.modules`` hit.
    """
    try:
        from pyatlan_v9.model.assets import (  # noqa: PLC0415 — deferred: optional dep
            Asset,
        )
        from pyatlan_v9.model.transform import (  # type: ignore[import]  # noqa: PLC0415 — deferred: optional dep
            to_atlas_format,
        )
    except ImportError:
        return None

    if not isinstance(asset, Asset):
        return None
    result: dict[str, Any] = to_atlas_format(asset)
    return result


def _set_sql_dialect(entity: dict[str, Any], dialect: str) -> None:
    """Stamp ``attributes.sqlDialect`` when the entity carries DDL.

    Conditional because the attribute means "parse this asset's definition as
    *dialect*" — on an asset with no definition it is noise that every diff
    downstream has to carry. A value the mapper set already wins, on the same
    rule as ``connectionName``: a mapper that set it deliberately is the
    authority.
    """
    attributes = entity.get("attributes")
    if not isinstance(attributes, dict):
        return
    if not any(attributes.get(key) for key in _DEFINITION_KEYS):
        return
    attributes.setdefault("sqlDialect", dialect)


def apply_envelope(
    entity: dict[str, Any],
    *,
    policy: EntityEnvelopePolicy,
    decorations: EntityDecorations | None = None,
    already_flattened: bool = False,
) -> dict[str, Any]:
    """Apply *policy* and *decorations* to one serialised entity dict.

    Args:
        entity: The serialised entity. Mutated in place.
        policy: The connector's declared envelope policy.
        decorations: Top-level contract fields to add, if any.
        already_flattened: True when *entity* came from ``to_atlas_format``,
            which has done the flattening itself. Skips
            :func:`flatten_envelope` rather than re-running a no-op over every
            record.

    Returns:
        The finished entity dict, ready to serialise.
    """
    if policy.shape is EnvelopeShape.FLATTENED and not already_flattened:
        entity = flatten_envelope(entity)

    if policy.sql_dialect is not None:
        _set_sql_dialect(entity, policy.sql_dialect)

    if decorations is not None:
        entity.update(decorations.as_root_fields())

    return entity
