"""Generic transform-output invariants for integration testing.

A connector's extract -> transform stage is a deterministic function that ends at
one artifact: the ``transformed/<typeName>/*.json`` output each entity is written
to (newline-delimited JSON, one Atlan entity per line). These invariants assert
properties that must hold of that artifact for *any* source or tenant — they need
no captured golden and no hand-written schema, so they are the cheapest way to
turn "the workflow ran" into "the workflow produced well-formed output".

Each invariant is declarative and independent of the connector. Declare them on a
workflow scenario::

    Scenario(
        name="crawl produces well-formed assets",
        api="workflow",
        entrypoint="crawler",
        assert_that={"success": is_true()},
        invariants=[
            UniqueQualifiedName(),
            NonEmptyOutput(),
            RequiredAttributes(),
        ],
    )

The runner loads the transformed output after the workflow completes and enforces
every declared invariant (see ``runner._validate_invariants``). Unlike the
warn-first asset backbone, a violated invariant fails the scenario: it was
declared because it must hold.

The module imports no heavy dependency at load time — output is read with
``orjson`` (line-delimited JSON), and pandas is imported lazily only when a
Parquet output file is present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from glob import glob
from typing import Any

import orjson

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


# =============================================================================
# Loading transformed entities
# =============================================================================


def _expand(obj: Any) -> list[dict[str, Any]]:
    """Normalise one parsed JSON value into a list of entity dicts.

    Accepts a bare entity, a list of entities, or an ``{"entities": [...]}``
    envelope — the three shapes connectors write.
    """
    if isinstance(obj, dict):
        inner = obj.get("entities")
        if isinstance(inner, list):
            return [e for e in inner if isinstance(e, dict)]
        return [obj]
    if isinstance(obj, list):
        return [e for e in obj if isinstance(e, dict)]
    return []


def _read_json_file(path: str) -> list[dict[str, Any]]:
    """Read one output file as newline-delimited JSON, falling back to whole-file."""
    raw = open(path, "rb").read()  # noqa: SIM115
    entities: list[dict[str, Any]] = []
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    try:
        for line in lines:
            entities.extend(_expand(orjson.loads(line)))
        return entities
    except orjson.JSONDecodeError:
        # Not NDJSON — try the file as a single JSON document (array or envelope).
        return _expand(orjson.loads(raw))


def load_entities(transformed_path: str) -> list[dict[str, Any]]:
    """Load every transformed entity under a directory tree.

    Reads ``*.json`` natively and ``*.parquet`` via a lazy pandas import, so
    importing this module never pulls in pandas. Nested ``attributes`` are
    preserved (unlike a flattened DataFrame), which is what per-entity invariants
    need.
    """
    entities: list[dict[str, Any]] = []
    for path in sorted(glob(f"{transformed_path}/**/*.json", recursive=True)):
        entities.extend(_read_json_file(path))

    parquet_files = sorted(glob(f"{transformed_path}/**/*.parquet", recursive=True))
    if parquet_files:
        import pandas as pd  # noqa: PLC0415

        for path in parquet_files:
            entities.extend(pd.read_parquet(path).to_dict(orient="records"))
    return entities


def _attributes(entity: dict[str, Any]) -> dict[str, Any]:
    attrs = entity.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _qualified_name(entity: dict[str, Any]) -> str | None:
    qn = _attributes(entity).get("qualifiedName") or entity.get("qualifiedName")
    return qn if isinstance(qn, str) else None


def _type_name(entity: dict[str, Any]) -> str | None:
    tn = entity.get("typeName") or entity.get("type_name")
    return tn if isinstance(tn, str) else None


# =============================================================================
# Invariants
# =============================================================================


class Invariant:
    """A named property that must hold of a connector's transformed output.

    Subclasses implement :meth:`check`, returning one human-readable message per
    violation (an empty list means the invariant holds).
    """

    name: str = "invariant"

    def check(self, entities: list[dict[str, Any]]) -> list[str]:  # pragma: no cover
        raise NotImplementedError


class UniqueQualifiedName(Invariant):
    """No two entities of the same type share a ``qualifiedName``.

    Duplicate qualifiedNames are produced by extraction SQL that fans out rows
    (e.g. an over-broad join), and are otherwise only caught downstream by the
    publish app's dedup pass.
    """

    name = "unique_qualified_name"

    def check(self, entities: list[dict[str, Any]]) -> list[str]:
        counts: dict[tuple[str | None, str], int] = {}
        for entity in entities:
            qn = _qualified_name(entity)
            if qn is None:
                continue
            counts[(_type_name(entity), qn)] = (
                counts.get((_type_name(entity), qn), 0) + 1
            )
        return [
            f"{type_name or '?'} qualifiedName appears {n}x (must be unique): {qn}"
            for (type_name, qn), n in counts.items()
            if n > 1
        ]


class NonEmptyOutput(Invariant):
    """The transform produced at least ``min_count`` entities (optionally of one type).

    A non-empty source that yields an empty transform output is a silent
    extraction failure that a bare ``success == true`` assertion never sees.
    """

    name = "non_empty_output"

    def __init__(self, min_count: int = 1, type_name: str | None = None) -> None:
        self.min_count = min_count
        self.type_name = type_name

    def check(self, entities: list[dict[str, Any]]) -> list[str]:
        pool = [
            e
            for e in entities
            if self.type_name is None or _type_name(e) == self.type_name
        ]
        if len(pool) < self.min_count:
            what = self.type_name or "entities"
            return [
                f"expected >= {self.min_count} {what} in transformed output, "
                f"found {len(pool)}"
            ]
        return []


class RequiredAttributes(Invariant):
    """Every entity has a ``typeName`` and the given attributes (default ``qualifiedName``).

    A missing qualifiedName or typeName produces an asset the metastore cannot
    place — it is dropped or orphaned at publish time.
    """

    name = "required_attributes"

    def __init__(self, *attributes: str) -> None:
        self.attributes = attributes or ("qualifiedName",)

    def check(self, entities: list[dict[str, Any]]) -> list[str]:
        violations: list[str] = []
        for index, entity in enumerate(entities):
            type_name = _type_name(entity)
            if not type_name:
                violations.append(f"entity #{index} has no typeName")
            attrs = _attributes(entity)
            for attribute in self.attributes:
                value = attrs.get(attribute, entity.get(attribute))
                if value in (None, ""):
                    ident = _qualified_name(entity) or f"#{index}"
                    violations.append(
                        f"{type_name or '?'} {ident} missing required attribute "
                        f"'{attribute}'"
                    )
        return violations


class QualifiedNamePrefix(Invariant):
    """Every entity's ``qualifiedName`` is prefixed by the connection's.

    Catches a transformer that builds qualifiedNames under the wrong connection
    (e.g. a hardcoded connector name), which orphans every asset it emits.
    """

    name = "qualified_name_prefix"

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def check(self, entities: list[dict[str, Any]]) -> list[str]:
        return [
            f"{_type_name(e) or '?'} qualifiedName not under '{self.prefix}': {qn}"
            for e in entities
            if (qn := _qualified_name(e)) is not None and not qn.startswith(self.prefix)
        ]


class AttributeNotNull(Invariant):
    """A given attribute is present and non-null on every entity of ``type_name``.

    Useful for aggregates the transform computes (e.g. a schema's tableCount),
    which regress to null on edge cases such as an empty schema.
    """

    name = "attribute_not_null"

    def __init__(self, type_name: str, attribute: str) -> None:
        self.type_name = type_name
        self.attribute = attribute

    def check(self, entities: list[dict[str, Any]]) -> list[str]:
        violations: list[str] = []
        for entity in entities:
            if _type_name(entity) != self.type_name:
                continue
            if _attributes(entity).get(self.attribute) is None:
                ident = _qualified_name(entity) or "?"
                violations.append(
                    f"{self.type_name} {ident} has null '{self.attribute}'"
                )
        return violations


# =============================================================================
# Running invariants
# =============================================================================


@dataclass
class InvariantReport:
    """Outcome of running a set of invariants over one workflow's output."""

    total_entities: int
    results: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(not violations for _, violations in self.results)

    @property
    def violation_count(self) -> int:
        return sum(len(violations) for _, violations in self.results)

    def format_report(self, max_per_invariant: int = 10) -> str:
        lines = [
            f"Invariant check over {self.total_entities} transformed entities:",
        ]
        for name, violations in self.results:
            status = "PASS" if not violations else f"FAIL ({len(violations)})"
            lines.append(f"  [{status}] {name}")
            for violation in violations[:max_per_invariant]:
                lines.append(f"       - {violation}")
            if len(violations) > max_per_invariant:
                lines.append(
                    f"       ... and {len(violations) - max_per_invariant} more"
                )
        return "\n".join(lines)


def check_invariants(
    transformed_path: str, invariants: list[Invariant]
) -> InvariantReport:
    """Load the transformed output and run every invariant against it."""
    if not os.path.isdir(transformed_path):
        return InvariantReport(
            total_entities=0,
            results=[
                (
                    getattr(inv, "name", "invariant"),
                    [f"transformed output path not found: {transformed_path}"],
                )
                for inv in invariants
            ],
        )
    entities = load_entities(transformed_path)
    results = [
        (getattr(inv, "name", "invariant"), inv.check(entities)) for inv in invariants
    ]
    return InvariantReport(total_entities=len(entities), results=results)
