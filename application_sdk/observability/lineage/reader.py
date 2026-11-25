"""Read what the app already wrote for publish and say, per asset, what is there.

Every connector app hands publish the same thing: NDJSON entities with a
``typeName`` and ``attributes.qualifiedName``, and ``Process`` / ``ColumnProcess``
entities whose inputs carry ``arsIdentity`` when the asset lookup still has to
resolve them. So the asset list, the Processes and the lookup refs need no code
inside the app's lineage step: they are read from those files afterwards.

The app declares, as data (:class:`CoverageSpec`):

- which asset types should have lineage and how lineage shows for each
  (a Process that outputs the asset, or a relationship attribute on the entity);
- which types are fields, and how a field names its parent;
- the key its recorder calls use, when that is not the qualifiedName.

Two streaming passes. Memory is one small entry per Process output and per
parent asset, plus an 8-byte digest per field that has lineage.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, BinaryIO

import orjson

from application_sdk.observability.lineage.identity import (
    IDENTITY_SCHEMA_VERSION,
    components_hash,
)
from application_sdk.observability.lineage.taxonomy import (
    GenericConnectionType,
    LineageReason,
    NonSqlSource,
)

__all__ = [
    "SQL_SYSTEM_SCHEMAS",
    "AssetRule",
    "CoverageSpec",
    "DerivedAsset",
    "FieldRule",
    "ReadStats",
    "read_run_output",
]

#: Warehouse system schemas Atlan does not catalog; a ref into one never resolves.
SQL_SYSTEM_SCHEMAS = frozenset({"INFORMATION_SCHEMA", "PG_CATALOG", "SYS"})

_PROCESS = "Process"
_COLUMN_PROCESS = "ColumnProcess"


@dataclass(frozen=True)
class AssetRule:
    """One asset type that should have lineage.

    Args:
        type_name: The entity ``typeName``.
        relationship_attributes: Attributes whose non-empty value is lineage for
            this type (apps that link BI assets by relationship, not Process).
            An asset with a Process output also counts.
        inbound_from: ``(typeName, attribute)`` pairs: an entity of that type
            naming this asset in that attribute is lineage for this asset (an
            embedded datasource naming its workbook, say).
    """

    type_name: str
    relationship_attributes: tuple[str, ...] = ()
    inbound_from: tuple[tuple[str, str], ...] = ()
    #: Entity attributes copied onto the asset (and its row) for the app's own
    #: rules and for readers. Keep these few and small: one copy per asset.
    keep_attributes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldRule:
    """A field type rolled up into its parent's row.

    Args:
        type_name: The field entity ``typeName``.
        parent_attributes: Attributes that hold the parent's qualifiedName, tried
            in order (a string, or an object with ``uniqueAttributes``). When
            empty or absent on the entity, the parent is the field's own
            qualifiedName minus its last ``/`` segment.
    """

    type_name: str
    parent_attributes: tuple[str, ...] = ()


def _qualified_name_key(type_name: str, qualified_name: str) -> str:
    return qualified_name


@dataclass(frozen=True)
class CoverageSpec:
    """What an app declares so its output can be read for coverage."""

    app_name: str
    assets: tuple[AssetRule, ...]
    fields: tuple[FieldRule, ...] = ()
    #: Connector types whose refs the asset lookup can resolve. Empty: all can.
    sql_connectors: frozenset[str] = frozenset()
    #: Connector types too generic to resolve (``odbc``, ``jdbc`` and the like).
    generic_connectors: frozenset[str] = frozenset()
    system_schemas: frozenset[str] = SQL_SYSTEM_SCHEMAS
    #: The reason for an asset whose refs are all on sources the lookup can't
    #: resolve, and for one on a generic connector. An app may restate either.
    non_sql_reason: type[LineageReason] = NonSqlSource
    generic_reason: type[LineageReason] = GenericConnectionType
    #: Recorder key for an asset: ``(typeName, qualifiedName) -> key``.
    key_for: Callable[[str, str], str] = _qualified_name_key

    def ref_resolvable(self, components: Mapping[str, Any]) -> bool:
        connector = str(components.get("connectorType") or "").lower()
        schema = str(components.get("schemaName") or "").upper()
        if self.sql_connectors and connector not in self.sql_connectors:
            return False
        return schema not in self.system_schemas


@dataclass
class DerivedAsset:
    """What the output says about one asset. No verdict yet."""

    type_name: str
    qualified_name: str
    name: str | None = None
    processes: int = 0
    process_qualified_name: str | None = None
    inputs: int = 0
    refs: int = 0
    resolvable_refs: int = 0
    ref_connectors: frozenset[str] = frozenset()
    has_relationship: bool = False
    fields_total: int = 0
    fields_with_lineage: int = 0
    fields_pending: int = 0
    column_refs: int = 0
    resolvable_column_refs: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def has_lineage(self) -> bool:
        return self.processes > 0 or self.has_relationship


@dataclass
class ReadStats:
    files: int = 0
    entities: int = 0
    processes: int = 0
    column_processes: int = 0
    refs: int = 0
    unreadable_lines: int = 0
    process_outputs_without_an_asset: int = 0


@dataclass
class _Out:
    processes: int = 0
    first: str | None = None
    inputs: int = 0
    refs: int = 0
    resolvable: int = 0
    connectors: set[str] = field(default_factory=set)


def _digest(qualified_name: str) -> bytes:
    return hashlib.blake2b(qualified_name.encode("utf-8"), digest_size=8).digest()


def _lines(path: str, stats: ReadStats | None) -> Iterator[dict[str, Any]]:
    with open(path, "rb") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                record = orjson.loads(line)
            except orjson.JSONDecodeError:
                if stats is not None:
                    stats.unreadable_lines += 1
                continue
            if isinstance(record, dict):
                yield record


def _ref_qn(ref: Any) -> str | None:
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict):
        unique = ref.get("uniqueAttributes")
        if isinstance(unique, dict):
            return unique.get("qualifiedName")
    return None


def _non_empty(value: Any) -> bool:
    """A relationship attribute that names at least one asset."""
    if isinstance(value, dict):
        return bool(_ref_qn(value) or value.get("guid"))
    if isinstance(value, (list, tuple)):
        return any(_non_empty(item) for item in value)
    return bool(value)


def read_run_output(
    paths: Iterable[str],
    spec: CoverageSpec,
    refs_out: BinaryIO | None = None,
    stats: ReadStats | None = None,
) -> Iterator[DerivedAsset]:
    """Yield one :class:`DerivedAsset` per asset of a declared type.

    Args:
        paths: The NDJSON files the app wrote for publish (entities and Processes).
        spec: The app's declaration.
        refs_out: Open binary file; one ref row per lookup ref is written to it.
        stats: Filled in as files are read.
    """
    paths = list(paths)
    stats = stats if stats is not None else ReadStats()
    asset_rules = {rule.type_name: rule for rule in spec.assets}
    field_rules = {rule.type_name: rule for rule in spec.fields}

    outputs: dict[str, _Out] = {}
    # digest of a field with lineage -> [lookup refs, resolvable lookup refs]
    field_lineage: dict[bytes, list[int]] = {}
    for path in paths:
        stats.files += 1
        for record in _lines(path, stats):
            type_name = record.get("typeName")
            if type_name not in (_PROCESS, _COLUMN_PROCESS):
                continue
            attributes = record.get("attributes") or {}
            process_qn = attributes.get("qualifiedName")
            identities = [
                (item.get("attributes") or {}).get("arsIdentity")
                for item in attributes.get("inputs") or []
                if isinstance(item, dict)
            ]
            identities = [item for item in identities if item]
            targets = [_ref_qn(o) for o in attributes.get("outputs") or []]
            if type_name == _COLUMN_PROCESS:
                stats.column_processes += 1
                resolvable_count = sum(
                    spec.ref_resolvable(identity.get("components") or {})
                    for identity in identities
                )
                for target in targets:
                    if target:
                        counts = field_lineage.setdefault(_digest(target), [0, 0])
                        counts[0] += len(identities)
                        counts[1] += resolvable_count
                continue
            stats.processes += 1
            for target in targets:
                if not target:
                    continue
                out = outputs.get(target)
                if out is None:
                    out = outputs[target] = _Out()
                out.processes += 1
                out.first = out.first or process_qn
                out.inputs += len(attributes.get("inputs") or [])
                for identity in identities:
                    components = identity.get("components") or {}
                    resolvable = spec.ref_resolvable(components)
                    out.refs += 1
                    out.resolvable += resolvable
                    out.connectors.add(
                        str(components.get("connectorType") or "").lower()
                    )
                    stats.refs += 1
                    if refs_out is not None:
                        refs_out.write(
                            orjson.dumps(
                                {
                                    "qualifiedName": target,
                                    "processQualifiedName": process_qn,
                                    "componentsHash": components_hash(components),
                                    "identityVersion": IDENTITY_SCHEMA_VERSION,
                                    "components": components,
                                    "matchTypeNames": identity.get("matchTypeNames"),
                                    "noMatchAction": identity.get("noMatchAction"),
                                    "expected": resolvable,
                                }
                            )
                            + b"\n"
                        )

    inbound: dict[str, list[str]] = {}  # source typeName -> attributes to read
    for rule in spec.assets:
        for source_type, attribute in rule.inbound_from:
            inbound.setdefault(source_type, []).append(attribute)
    named: set[str] = set()  # qualifiedNames some entity names as its target
    parents: dict[str, DerivedAsset] = {}
    # parent qn -> [fields, with lineage, pending, column refs, resolvable column refs]
    field_counts: dict[str, list[int]] = {}
    for path in paths:
        for record in _lines(path, None):
            type_name = record.get("typeName")
            if type_name in (_PROCESS, _COLUMN_PROCESS) or not type_name:
                continue
            attributes = record.get("attributes") or {}
            qualified_name = attributes.get("qualifiedName")
            if not qualified_name:
                continue
            stats.entities += 1
            for attribute in inbound.get(type_name, ()):
                value = attributes.get(attribute)
                for item in value if isinstance(value, list) else [value]:
                    target = _ref_qn(item)
                    if target:
                        named.add(target)
            field_rule = field_rules.get(type_name)
            if field_rule is not None:
                parent = None
                for name in field_rule.parent_attributes:
                    parent = _ref_qn(attributes.get(name))
                    if parent:
                        break
                parent = parent or qualified_name.rsplit("/", 1)[0]
                counts = field_counts.get(parent)
                if counts is None:
                    counts = field_counts[parent] = [0, 0, 0, 0, 0]
                counts[0] += 1
                lineage = field_lineage.get(_digest(qualified_name))
                if lineage is not None:
                    counts[1] += 1
                    counts[2] += lineage[0] > 0
                    counts[3] += lineage[0]
                    counts[4] += lineage[1]
                continue
            rule = asset_rules.get(type_name)
            if rule is None:
                continue
            asset = parents.get(qualified_name)
            if asset is None:
                asset = parents[qualified_name] = DerivedAsset(
                    type_name=type_name, qualified_name=qualified_name
                )
            asset.name = asset.name or attributes.get("name")
            for name in rule.keep_attributes:
                if name in attributes and name not in asset.attributes:
                    asset.attributes[name] = attributes[name]
            # An app may write one asset in several files (entity, relationships).
            asset.has_relationship = asset.has_relationship or any(
                _non_empty(attributes.get(name))
                for name in rule.relationship_attributes
            )

    for qualified_name, asset in parents.items():
        if qualified_name in named and asset_rules[asset.type_name].inbound_from:
            asset.has_relationship = True
        out = outputs.pop(qualified_name, None)
        if out is not None:
            asset.processes = out.processes
            asset.process_qualified_name = out.first
            asset.inputs = out.inputs
            asset.refs = out.refs
            asset.resolvable_refs = out.resolvable
            asset.ref_connectors = frozenset(out.connectors)
        counts = field_counts.get(qualified_name)
        if counts is not None:
            (
                asset.fields_total,
                asset.fields_with_lineage,
                asset.fields_pending,
                asset.column_refs,
                asset.resolvable_column_refs,
            ) = counts
        yield asset
    stats.process_outputs_without_an_asset = len(outputs)
