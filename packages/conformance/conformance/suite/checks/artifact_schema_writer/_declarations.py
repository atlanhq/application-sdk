"""Read every committed artifact-schema declaration in the generated tree.

``artifactSchemas`` is authored in the app's pkl contract and rendered by the
toolkit to ``artifact_schemas.json``, keyed by the name of the ``FileReference``
contract field each entry describes.  Like every other contract-side read in
this suite, K017 reads the *committed* generated artifact rather than the pkl
source: it avoids requiring the ``pkl`` CLI at check time and matches what the
runtime actually loads.  Freshness is C002's job.

**Why this reader is app-wide where K016's is per-entry-point.**  K016 asks "is
*this entry point's* boundary field declared", so it must resolve which
declarations file speaks for which entry point and stay silent when it cannot.
K017 asks a different question — "for a field the app has already declared,
does its writer agree?" — which is keyed on the declaration, not on the entry
point.  Every ``artifact_schemas.json`` under ``app/generated/`` is therefore
merged into one field -> declaration map.

**A field declared two ways is dropped, not merged.**  In a multi-entry-point
bundle the same field name can appear in two entry points' files.  If both
agree, either answers.  If they disagree — different format, different field
list — there is no single declaration the writer can be measured against, so the
key is removed entirely.  Reporting against an arbitrary one of them would be a
coin flip, and a WARN rule prefers a false negative to a false positive.

A malformed file contributes nothing rather than contributing "declares
nothing": a bad JSON blob is C002/K005 territory, not this rule's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ARTIFACT_SCHEMAS_FILENAME = "artifact_schemas.json"

#: The artifact formats the SDK's validation wrapper understands
#: (``application_sdk.validation.artifacts.ARTIFACT_FORMATS``).  A declaration
#: naming anything else is vocabulary this check does not know, so it is dropped
#: rather than compared against.
DECLARED_FORMATS: frozenset[str] = frozenset({"ndjson", "parquet"})


@dataclass(frozen=True)
class ArtifactDeclaration:
    """One entry of a committed ``artifact_schemas.json``."""

    field: str
    """The ``FileReference`` contract field name this entry is keyed by."""

    format: str
    """Declared artifact format — one of :data:`DECLARED_FORMATS`."""

    top_level_fields: frozenset[str]
    """Declared field names, truncated to their top-level segment.

    Declarations address nested NDJSON shapes by path
    (``attributes.columns[].name``); a writer's record class only ever exposes
    the top-level attribute, so the comparison is made at that level.  Anything
    deeper is the runtime validator's job, not a static check's.
    """

    source: str
    """Repo-relative path of the file that declared it, for the finding text."""


def _top_level(name: str) -> str:
    """Return the top-level segment of a declared field path."""
    return name.split(".", 1)[0].split("[", 1)[0]


def _read_one(path: Path, root: Path) -> list[ArtifactDeclaration]:
    """Parse one ``artifact_schemas.json``; return ``[]`` for anything unreadable."""
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    schemas = data.get("schemas") if isinstance(data, dict) else None
    if not isinstance(schemas, dict):
        return []

    try:
        source = str(path.relative_to(root))
    except ValueError:  # pragma: no cover — path is built from root
        source = str(path)

    result: list[ArtifactDeclaration] = []
    for field, spec in schemas.items():
        if not isinstance(spec, dict):
            continue
        fmt = spec.get("format")
        if not isinstance(fmt, str) or fmt not in DECLARED_FORMATS:
            continue
        raw_fields = spec.get("fields")
        if not isinstance(raw_fields, list):
            continue
        names: set[str] = set()
        for entry in raw_fields:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.add(_top_level(entry["name"]))
        if not names:
            continue
        result.append(
            ArtifactDeclaration(
                field=str(field),
                format=fmt,
                top_level_fields=frozenset(names),
                source=source,
            )
        )
    return result


def read_declarations(root: Path) -> dict[str, ArtifactDeclaration]:
    """Merge every ``artifact_schemas.json`` under ``app/generated/`` by field name.

    Args:
        root: Repo root.

    Returns:
        Field name -> declaration.  Empty when there is no generated tree, no
        declarations file, or nothing in them this check understands.  A field
        two files declare differently is absent from the result.
    """
    generated = root / "app" / "generated"
    if not generated.is_dir():
        return {}

    merged: dict[str, ArtifactDeclaration] = {}
    conflicting: set[str] = set()
    for path in sorted(generated.rglob(ARTIFACT_SCHEMAS_FILENAME)):
        for decl in _read_one(path, root):
            existing = merged.get(decl.field)
            if existing is None:
                merged[decl.field] = decl
            elif (existing.format, existing.top_level_fields) != (
                decl.format,
                decl.top_level_fields,
            ):
                conflicting.add(decl.field)

    for field in conflicting:
        merged.pop(field, None)
    return merged
