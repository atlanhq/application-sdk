"""The 1-to-N relationship table P055 reads, baked into the wheel (FND-3490).

Publish orders entities by type using the typedef relationships.  In a 1-to-N
relationship it treats the "1" side as the parent and sends it first (Table
before Column), relying on the "N" side to carry the reference back to its
single parent (``Column.table``).  A connector that writes the link from the
"1" side instead — populating the list end (``Table.columns``) — makes the
parent name children that do not exist yet, and Atlas rejects it with
``ATLAS-404-00-00A``.

P055 flags a mapper that populates such a list end.  To know which list ends
those are it needs relationship cardinality, which lives in the typedefs.  The
suite runs inside consumer app repos from an isolated environment without
pyatlan, so the table ships as committed data — the same mechanism as the
deprecation manifest, the public-error allowlist and the SDK type-alias table:

    uv run --directory packages/conformance --extra test atlan-application-sdk-conformance gen-relationship-directions

reads ``pyatlan_v9.model.assets`` at SDK-dev time; ``--check`` and a drift test
keep the committed JSON equal to the pinned pyatlan.

Pairing
-------
pyatlan_v9 records each end's cardinality (``Related<X>`` vs
``List[Related<X>]``) but not which two attributes form one relationship.  A
relationship is declared between two types, so pairing is done on the fields
each class *declares* — those absent from its supertype — rather than the fields
it inherits.  Without that, ``FabricActivity`` would offer three ``Process``
references (its own ``fabric_process`` plus the ``input_to_processes`` /
``output_from_processes`` every ``Catalog`` inherits) and the pairing would be
ambiguous.

``(X.a, Y.b)`` is a 1-to-N pair when ``a`` is the only field ``X`` declares that
references ``Y``, it is a list, and ``b`` is the only field ``Y`` declares that
references ``X``, and it is single-valued.  For a self-referencing type
(``X == Y``) the declared fields must be exactly one list and one single.
Anything else is left out, so an ambiguous shape is a missed finding rather than
a false one.  Each pair is then recorded for ``X`` and every subtype of ``X``
that carries ``a`` — including a multiply-inherited one (``DbtProcess``) the
Related MRO does not show.

Checked against the ``atlanhq/models`` relationshipDefs this pairing derives 298
1-to-N relationships with no wrong pair.  The 1-to-N relationships it cannot
disambiguate from types alone — ``Database`` declares two ``Schema`` lists, the
1-to-N ``schemas`` and the many-to-many ``sqlSchemas`` — are listed in
:data:`_TYPEDEF_PAIRS`, copied from those typedefs.  The builder verifies each
against pyatlan_v9 and raises if one no longer matches, so a pyatlan change that
breaks an entry fails the generator rather than shipping a wrong pair.
"""

from __future__ import annotations

import importlib.resources as _ir
import json
import typing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DATA_RELPATH: tuple[str, ...] = ("data", "relationship_directions.json")

_ASSETS_MODULE = "pyatlan_v9.model.assets"
_RELATED_PREFIX = "Related"

#: ``(owner typeName, list attribute)`` pairs that are 1-to-N by shape but where
#: populating the list end is legitimate.  Empty until a real case needs one —
#: add the pair with a comment naming why.
_EXEMPT: frozenset[tuple[str, str]] = frozenset()

#: 1-to-N relationships the type-based pairing cannot disambiguate, from the
#: ``atlanhq/models`` relationshipDefs (``atlas/entityDefs``, commit fab0a364):
#: ``(owner type, list attribute, target type, single attribute on target)``.
_TYPEDEF_PAIRS: tuple[tuple[str, str, str, str], ...] = (
    ("Asset", "dqBaseDatasetRules", "DataQualityRule", "dqRuleBaseDataset"),
    ("Column", "dbtModelColumns", "DbtModelColumn", "sqlColumn"),
    ("Column", "dqBaseColumnRules", "DataQualityRule", "dqRuleBaseColumn"),
    ("Column", "foreignKeyTo", "Column", "foreignKeyFrom"),
    ("Column", "metricTimestamps", "Metric", "metricTimestampColumn"),
    ("Column", "nestedColumns", "Column", "parentColumn"),
    ("CosmosMongoDBCollection", "columns", "Column", "cosmosMongoDBCollection"),
    ("CustomEntity", "customChildEntities", "CustomEntity", "customParentEntity"),
    ("Database", "schemas", "Schema", "database"),
    (
        "FlowControlOperation",
        "flowControlledOperations",
        "FlowControlOperation",
        "flowControlledBy",
    ),
    ("FlowReusableUnit", "flowAbstracts", "FlowDataset", "flowDetailedBy"),
    ("FlowReusableUnit", "flowDatasets", "FlowDataset", "flowParentUnit"),
    ("MCMonitor", "mcIncidents", "MCIncident", "mcMonitor"),
    (
        "ModelAttribute",
        "modelAttributeRelatedFromAttributes",
        "ModelAttributeAssociation",
        "modelAttributeAssociationTo",
    ),
    (
        "ModelAttribute",
        "modelAttributeRelatedToAttributes",
        "ModelAttributeAssociation",
        "modelAttributeAssociationFrom",
    ),
    (
        "ModelEntity",
        "modelEntityRelatedFromEntities",
        "ModelEntityAssociation",
        "modelEntityAssociationTo",
    ),
    (
        "ModelEntity",
        "modelEntityRelatedToEntities",
        "ModelEntityAssociation",
        "modelEntityAssociationFrom",
    ),
    (
        "ModelEntity",
        "modelEntitySpecializationEntities",
        "ModelEntity",
        "modelEntityGeneralizationEntity",
    ),
    ("MongoDBCollection", "mongoDBColumns", "Column", "mongoDBCollection"),
    ("SQL", "dbtModels", "DbtModel", "sqlAsset"),
    ("SQL", "dbtSources", "DbtSource", "sqlAsset"),
    ("SQL", "sqlInsightIncomingJoins", "SqlInsightJoin", "sqlInsightJoinedDataset"),
    ("SQL", "sqlInsightOutgoingJoins", "SqlInsightJoin", "sqlInsightSourceDataset"),
    ("SalesforceObject", "fields", "SalesforceField", "object"),
    # Schema.snowflakeDynamicTables <-> SnowflakeDynamicTable.atlanSchema is left
    # out: pyatlan_v9's SnowflakeDynamicTable has no atlanSchema field, so a typed
    # mapper has no child-side reference to set instead.  The same holds for
    # SnowflakeDynamicTable.columns, which pyatlan_v9 does not expose.
    # TableauDatasource.fields and TableauProject.metrics are left out because
    # pyatlan_v9 types their elements (TableauDatasourceField, Metric) wider than
    # the typedef end, so the pair cannot be verified against the model.
    (
        "SnowflakeSemanticView",
        "snowflakeSemanticLogicalTables",
        "SnowflakeSemanticLogicalTable",
        "snowflakeSemanticView",
    ),
)


@dataclass(frozen=True)
class SetEnd:
    """The list end of a 1-to-N relationship, as seen from its owner type."""

    attribute: str
    """The list end's Atlas attribute name, e.g. ``fabricActivities``."""
    target_type: str
    """The "N"-side type the list holds, e.g. ``FabricActivity``."""
    inverse: str
    """The single end's pyatlan_v9 field name on the target, e.g. ``fabric_process``."""
    inverse_attribute: str
    """The single end's Atlas attribute name, e.g. ``fabricProcess``."""


#: ``{owner typeName: {list field name: SetEnd}}``.
RelationshipTable = dict[str, dict[str, SetEnd]]


@dataclass(frozen=True)
class RelationshipData:
    """What P055 reads: the 1-to-N list ends, and the field names that are not
    always one.

    ``other_list_fields`` names every relationship list field that, on some
    type, is *not* a 1-to-N list end (``SalesforceDashboard.reports`` is
    many-to-many while ``SalesforceOrganization.reports`` is 1-to-N).  When
    P055 cannot resolve a receiver's type it judges by field name, and for these
    names the field alone does not say which relationship is meant.
    """

    set_ends: RelationshipTable
    other_list_fields: frozenset[str]


def _data_path() -> Path:
    return Path(str(_ir.files("conformance"))).joinpath(*_DATA_RELPATH)


DATA_PATH = _data_path()


@dataclass(frozen=True)
class _Ref:
    field: str
    attribute: str
    is_list: bool
    target: str


def _ref_of(annotation: object) -> tuple[bool, str] | None:
    """``(is_list, target asset name)`` for a relationship field annotation.

    pyatlan_v9 annotates relationship fields ``Union[Related<X>, None,
    UnsetType]`` or ``Union[List[Related<X>], None, UnsetType]``.
    """
    for arg in typing.get_args(annotation):
        if typing.get_origin(arg) is list:
            (elem,) = typing.get_args(arg)
            name = getattr(elem, "__name__", "")
            if name.startswith(_RELATED_PREFIX):
                return True, name[len(_RELATED_PREFIX) :]
        name = getattr(arg, "__name__", "")
        if isinstance(arg, type) and name.startswith(_RELATED_PREFIX):
            return False, name[len(_RELATED_PREFIX) :]
    return None


def build_relationship_data() -> RelationshipData:
    """Derive every 1-to-N list end from the installed ``pyatlan_v9``.

    Imports pyatlan_v9 lazily: only the generator and the drift test call this,
    never a scan.
    """
    import importlib

    import msgspec

    assets = importlib.import_module(_ASSETS_MODULE)
    referenceable = assets.Referenceable
    classes: dict[str, type] = {}
    for name in assets.__all__:
        obj = getattr(assets, name, None)
        if isinstance(obj, type) and issubclass(obj, referenceable):
            classes[name] = obj

    def related_cls(name: str) -> type | None:
        return getattr(assets, f"{_RELATED_PREFIX}{name}", None)

    def supertype(name: str) -> str | None:
        """Nearest ancestor that is itself an asset class, via the Related MRO."""
        rel = related_cls(name)
        if rel is None:
            return None
        for base in rel.__mro__[1:]:
            base_name = base.__name__[len(_RELATED_PREFIX) :]
            if base.__name__.startswith(_RELATED_PREFIX) and base_name in classes:
                return base_name
        return None

    def mro_subtype(sub: str, sup: str) -> bool:
        rel_sub, rel_sup = related_cls(sub), related_cls(sup)
        return (
            rel_sub is not None and rel_sup is not None and issubclass(rel_sub, rel_sup)
        )

    all_refs: dict[str, dict[str, _Ref]] = {}
    for name, cls in classes.items():
        refs: dict[str, _Ref] = {}
        for f in msgspec.structs.fields(cls):
            parsed = _ref_of(f.type)
            if parsed is not None and parsed[1] in classes:
                refs[f.name] = _Ref(f.name, f.encode_name, parsed[0], parsed[1])
        all_refs[name] = refs

    fields_of: dict[str, dict[str, object]] = {
        name: {f.name: f.type for f in msgspec.structs.fields(cls)}
        for name, cls in classes.items()
    }
    own_fields: dict[str, dict[str, object]] = {}
    for name in classes:
        parent = supertype(name)
        inherited_fields = fields_of[parent] if parent is not None else {}
        own_fields[name] = {
            f: t for f, t in fields_of[name].items() if f not in inherited_fields
        }

    def declared(name: str) -> list[_Ref]:
        parent = supertype(name)
        inherited = set(all_refs[parent]) if parent is not None else set()
        return [r for f, r in all_refs[name].items() if f not in inherited]

    def is_subtype(sub: str, sup: str) -> bool:
        """``sub`` is ``sup`` or derives from it.

        The Related MRO carries single inheritance only: ``DbtProcess`` is both
        a ``Dbt`` and a ``Process`` in the typedefs, but ``RelatedDbtProcess``
        derives from ``RelatedDbt`` alone.  So a type that carries every field
        ``sup`` declares — attributes as well as relationships, typed the same —
        counts as a subtype too.  Relationships alone are not enough: a type
        declaring a single relationship (``KafkaCluster.kafka_topics``) would
        match every type with a same-named list (``KafkaConsumerGroup``'s
        many-to-many ``kafka_topics``) — and a type declaring one field only
        (``AnaplanApp``) gives too little to tell, so it needs at least two.
        """
        if mro_subtype(sub, sup):
            return True
        own = own_fields[sup]
        return len(own) >= 2 and all(fields_of[sub].get(f) == t for f, t in own.items())

    table: RelationshipTable = {}

    pairs: list[tuple[str, str, SetEnd]] = []

    def record(x: str, set_field: str, entry: SetEnd) -> None:
        pairs.append((x, set_field, entry))

    for x in sorted(classes):
        own_x = declared(x)
        for y in sorted({r.target for r in own_x}):
            x_to_y = [r for r in own_x if r.target == y]
            if x == y:
                lists = [r for r in x_to_y if r.is_list]
                singles = [r for r in x_to_y if not r.is_list]
                if len(x_to_y) != 2 or len(lists) != 1 or len(singles) != 1:
                    continue
                set_end, single_end = lists[0], singles[0]
            else:
                y_to_x = [r for r in declared(y) if r.target == x]
                if len(x_to_y) != 1 or len(y_to_x) != 1:
                    continue
                set_end, single_end = x_to_y[0], y_to_x[0]
                if not set_end.is_list or single_end.is_list:
                    continue
            entry = SetEnd(
                attribute=set_end.attribute,
                target_type=y,
                inverse=single_end.field,
                inverse_attribute=single_end.attribute,
            )
            record(x, set_end.field, entry)

    for x, attribute, y, inverse_attribute in _TYPEDEF_PAIRS:
        if x not in classes or y not in classes:
            raise ValueError(f"_TYPEDEF_PAIRS: {x} or {y} is not a pyatlan_v9 asset")
        set_ref = next(
            (r for r in all_refs[x].values() if r.attribute == attribute), None
        )
        single_ref = next(
            (r for r in all_refs[y].values() if r.attribute == inverse_attribute), None
        )
        if (
            set_ref is None
            or not set_ref.is_list
            or not is_subtype(y, set_ref.target)
            or single_ref is None
            or single_ref.is_list
            or not is_subtype(x, single_ref.target)
        ):
            raise ValueError(
                f"_TYPEDEF_PAIRS: {x}.{attribute} (list) <-> {y}.{inverse_attribute} "
                f"(single) no longer matches pyatlan_v9 — re-check the typedef."
            )
        record(
            x,
            set_ref.field,
            SetEnd(
                attribute=attribute,
                target_type=y,
                inverse=single_ref.field,
                inverse_attribute=inverse_attribute,
            ),
        )
    # Expand each pair to its owner types.  Subtypes by the Related MRO come
    # first and win; a type that is only a structural subtype (multiple
    # inheritance) takes an end only where exactly one pair offers it one.
    for x, set_field, entry in pairs:
        for owner in sorted(classes):
            if mro_subtype(owner, x) and set_field in all_refs[owner]:
                table.setdefault(owner, {})[set_field] = entry
    structural: dict[tuple[str, str], set[SetEnd]] = {}
    for x, set_field, entry in pairs:
        for owner in sorted(classes):
            if (
                set_field in all_refs[owner]
                and set_field not in table.get(owner, {})
                and is_subtype(owner, x)
            ):
                structural.setdefault((owner, set_field), set()).add(entry)
    for (owner, set_field), entries in structural.items():
        if len(entries) == 1:
            table.setdefault(owner, {})[set_field] = next(iter(entries))
    for owner in list(table):
        for set_field in list(table[owner]):
            if (owner, table[owner][set_field].attribute) in _EXEMPT:
                del table[owner][set_field]
        if not table[owner]:
            del table[owner]

    other_list_fields = frozenset(
        field
        for owner, refs in all_refs.items()
        for field, ref in refs.items()
        if ref.is_list and field not in table.get(owner, {})
    )
    return RelationshipData(set_ends=table, other_list_fields=other_list_fields)


def serialize(data: RelationshipData) -> str:
    """Deterministic JSON so ``--check`` is a stable staleness gate.

    Grouped by list field, with the owner types that share one end listed
    together: a relationship declared on a supertype (``Asset.links``) applies to
    every subtype, and repeating the end per owner would multiply the file.
    """
    grouped: dict[str, dict[SetEnd, list[str]]] = {}
    for owner, fields in data.set_ends.items():
        for field, end in fields.items():
            grouped.setdefault(field, {}).setdefault(end, []).append(owner)
    payload = {
        field: sorted(
            (
                {
                    "attribute": end.attribute,
                    "inverse": end.inverse,
                    "inverse_attribute": end.inverse_attribute,
                    "owners": sorted(owners),
                    "target_type": end.target_type,
                }
                for end, owners in ends.items()
            ),
            key=lambda entry: (entry["target_type"], entry["inverse"]),
        )
        for field, ends in grouped.items()
    }
    document = {
        "other_list_fields": sorted(data.other_list_fields),
        "set_ends": payload,
    }
    return json.dumps(document, indent=1, sort_keys=True) + "\n"


@lru_cache(maxsize=1)
def load_relationship_data() -> RelationshipData:
    """Load the committed table, or an empty one when absent/unparseable.

    Returning empty (rather than raising) keeps the suite from crashing a
    consumer's CI if the baked data ever goes missing; P055 then reports
    nothing, and the drift test is what catches a missing file in this repo.
    """
    empty = RelationshipData(set_ends={}, other_list_fields=frozenset())
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return empty
    set_ends = data.get("set_ends") if isinstance(data, dict) else None
    other = data.get("other_list_fields") if isinstance(data, dict) else None
    if not isinstance(set_ends, dict) or not isinstance(other, list):
        return empty
    table: RelationshipTable = {}
    for field, entries in set_ends.items():
        if not isinstance(entries, list):
            continue
        for raw in entries:
            if not isinstance(raw, dict) or not isinstance(raw.get("owners"), list):
                continue
            try:
                end = SetEnd(
                    attribute=str(raw["attribute"]),
                    target_type=str(raw["target_type"]),
                    inverse=str(raw["inverse"]),
                    inverse_attribute=str(raw["inverse_attribute"]),
                )
            except KeyError:
                continue
            for owner in raw["owners"]:
                table.setdefault(str(owner), {})[str(field)] = end
    return RelationshipData(
        set_ends=table, other_list_fields=frozenset(str(f) for f in other)
    )
