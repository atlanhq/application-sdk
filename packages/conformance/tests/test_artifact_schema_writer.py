"""Tests for K017 ArtifactSchemaWriterMismatch.

Covers the declaration-vs-writer cross-check: for a ``FileReference`` contract
field the app has already declared in ``artifact_schemas.json``, the Python that
writes the artifact must agree with the declaration — the file extension must be
one the declared ``format`` can be, and the record class serialised into it must
not carry fields the declaration omits.

The rule's value is as much in what it stays quiet about as in what it reports,
so the "unresolvable shape" cases below are the load-bearing half of this file.

Test helpers
------------
``_write_py``: writes ``{relative_path: source_text}`` under ``tmp_path``.
``_write_schemas``: writes an ``artifact_schemas.json`` shaped like the toolkit's
real output, from ``{field: (format, [field names])}``.
``_run``: writes the Python, calls :func:`scan_all`, returns its findings.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.artifact_schema_writer import scan_all, scan_path
from conformance.suite.checks.artifact_schema_writer._declarations import (
    read_declarations,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_py(tmp_path: Path, py_files: dict[str, str]) -> list[Path]:
    paths: list[Path] = []
    for name, src in py_files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        paths.append(p)
    return paths


def _write_schemas(path: Path, schemas: dict[str, tuple[str, list[str]]]) -> None:
    """Write a well-formed declarations envelope from ``{field: (format, names)}``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "schemas": {
                    field: {
                        "format": fmt,
                        "description": f"Declared shape of {field}.",
                        "fields": [
                            {
                                "name": name,
                                "type": "string",
                                "required": True,
                                "description": f"The {name} field.",
                            }
                            for name in names
                        ],
                    }
                    for field, (fmt, names) in schemas.items()
                },
            }
        ),
        encoding="utf-8",
    )


def _run(tmp_path: Path, py_files: dict[str, str]) -> list:
    paths = _write_py(tmp_path, py_files)
    return scan_all(paths, tmp_path)


def _declare(tmp_path: Path, schemas: dict[str, tuple[str, list[str]]]) -> None:
    _write_schemas(tmp_path / "app" / "generated" / "artifact_schemas.json", schemas)


_NDJSON_WRITER = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class Entity:
    typeName: str
    qualifiedName: str


class TransformOutput(Output):
    transformed_entities: FileReference | None = None


def transform(out_dir: Path) -> TransformOutput:
    out_file = out_dir / "entities.jsonl"
    with out_file.open("wb") as handle:
        entity = Entity()
        handle.write(encode(entity))
    return TransformOutput(
        transformed_entities=FileReference(local_path=str(out_file)),
    )
"""


def _writer_source(*, filename: str, record_fields: str = "") -> str:
    """An NDJSON writer for ``transformed_entities`` writing to *filename*."""
    body = record_fields or "    typeName: str\n    qualifiedName: str"
    return f"""
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class Entity:
{body}


class TransformOutput(Output):
    transformed_entities: FileReference | None = None


def transform(out_dir: Path) -> TransformOutput:
    out_file = out_dir / "{filename}"
    with out_file.open("wb") as handle:
        entity = Entity()
        handle.write(encode(entity))
    return TransformOutput(
        transformed_entities=FileReference(local_path=str(out_file)),
    )
"""


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_k017_rule_metadata_matches_the_designed_disposition() -> None:
    rule = get_rule("K017")
    assert rule.name == "ArtifactSchemaWriterMismatch"
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.WARN
    assert rule.mechanism is RuleMechanism.STATIC
    assert rule.orthogonal_gate == "pkl-eval"
    assert rule.autofixable is False


def test_scan_path_is_a_no_op(tmp_path: Path) -> None:
    """Per-file scanning has no meaning for a cross-artifact rule."""
    assert scan_path(tmp_path / "app" / "main.py", tmp_path) == []


# ---------------------------------------------------------------------------
# Format vs extension
# ---------------------------------------------------------------------------


def test_a_parquet_declaration_with_an_ndjson_writer_is_reported(
    tmp_path: Path,
) -> None:
    _declare(
        tmp_path,
        {"transformed_entities": ("parquet", ["typeName", "qualifiedName"])},
    )

    findings = _run(
        tmp_path, {"app/main.py": _writer_source(filename="entities.jsonl")}
    )

    assert [f.rule_id for f in findings] == ["K017"]
    assert findings[0].file == "app/main.py"
    message = findings[0].message
    assert "'.jsonl'" in message
    assert 'format = "parquet"' in message
    assert 'format = "ndjson"' in message  # the remedy for the other side
    assert "app/generated/artifact_schemas.json" in message
    assert "# conformance: ignore[K017] <reason>" in message


def test_an_ndjson_declaration_with_a_parquet_writer_is_reported(
    tmp_path: Path,
) -> None:
    _declare(
        tmp_path,
        {"transformed_entities": ("ndjson", ["typeName", "qualifiedName"])},
    )

    findings = _run(
        tmp_path, {"app/main.py": _writer_source(filename="entities.parquet")}
    )

    assert len(findings) == 1
    assert "'.parquet'" in findings[0].message


def test_an_agreeing_writer_is_silent(tmp_path: Path) -> None:
    _declare(
        tmp_path,
        {"transformed_entities": ("ndjson", ["typeName", "qualifiedName"])},
    )

    assert _run(tmp_path, {"app/main.py": _writer_source(filename="x.ndjson")}) == []


def test_ndjson_written_to_a_json_path_is_silent(tmp_path: Path) -> None:
    """NDJSON on a ``.json`` path is ordinary in this fleet, not a defect."""
    _declare(
        tmp_path,
        {"transformed_entities": ("ndjson", ["typeName", "qualifiedName"])},
    )

    assert _run(tmp_path, {"app/main.py": _writer_source(filename="x.json")}) == []


def test_an_extension_the_rule_has_no_opinion_about_is_silent(
    tmp_path: Path,
) -> None:
    _declare(
        tmp_path,
        {"transformed_entities": ("parquet", ["typeName", "qualifiedName"])},
    )

    assert _run(tmp_path, {"app/main.py": _writer_source(filename="x.csv")}) == []


def test_a_directory_reference_is_silent(tmp_path: Path) -> None:
    """Partitioned parquet is a directory — it has no extension to disagree with."""
    _declare(tmp_path, {"raw_queries": ("parquet", ["QUERY_ID"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


def extract(out_dir: Path) -> ExtractOutput:
    part_dir = out_dir / "raw_queries"
    return ExtractOutput(raw_queries=FileReference.from_local(part_dir))
"""
    assert _run(tmp_path, {"app/main.py": src}) == []


def test_from_local_resolves_the_writers_extension(tmp_path: Path) -> None:
    _declare(tmp_path, {"raw_queries": ("parquet", ["QUERY_ID"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


def extract(out_dir: Path) -> ExtractOutput:
    out_file = out_dir / "queries.ndjson"
    return ExtractOutput(raw_queries=FileReference.from_local(out_file))
"""
    findings = _run(tmp_path, {"app/main.py": src})

    assert len(findings) == 1
    assert "'.ndjson'" in findings[0].message


def test_an_inline_path_literal_resolves(tmp_path: Path) -> None:
    """A reference built straight from a literal never passes through a variable."""
    _declare(tmp_path, {"raw_queries": ("parquet", ["QUERY_ID"])})
    src = """
from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


def extract() -> ExtractOutput:
    return ExtractOutput(
        raw_queries=FileReference(local_path="./local/tmp/queries.ndjson"),
    )
"""
    findings = _run(tmp_path, {"app/main.py": src})

    assert len(findings) == 1
    assert "'.ndjson'" in findings[0].message


def test_a_reference_assigned_to_a_local_still_binds(tmp_path: Path) -> None:
    _declare(tmp_path, {"raw_queries": ("parquet", ["QUERY_ID"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


def extract(out_dir: Path) -> ExtractOutput:
    out_file = out_dir / "queries.jsonl"
    ref = FileReference(local_path=str(out_file))
    return ExtractOutput(raw_queries=ref)
"""
    findings = _run(tmp_path, {"app/main.py": src})

    assert len(findings) == 1
    assert "'.jsonl'" in findings[0].message


def test_with_suffix_names_the_extension_directly(tmp_path: Path) -> None:
    """``with_suffix(".parquet")`` passes the extension itself, not a path."""
    _declare(tmp_path, {"raw_queries": ("ndjson", ["QUERY_ID"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


def extract(base: Path) -> ExtractOutput:
    out_file = base.with_suffix(".parquet")
    return ExtractOutput(raw_queries=FileReference.from_local(out_file))
"""
    findings = _run(tmp_path, {"app/main.py": src})

    assert len(findings) == 1
    assert "'.parquet'" in findings[0].message


def test_an_f_string_tail_carrying_only_the_extension_resolves(
    tmp_path: Path,
) -> None:
    """In ``f"{name}.jsonl"`` the trailing literal is a fragment, not a path."""
    _declare(tmp_path, {"raw_queries": ("parquet", ["QUERY_ID"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


def extract(out_dir: Path, name: str) -> ExtractOutput:
    out_file = out_dir / f"{name}.jsonl"
    return ExtractOutput(raw_queries=FileReference.from_local(out_file))
"""
    findings = _run(tmp_path, {"app/main.py": src})

    assert len(findings) == 1
    assert "'.jsonl'" in findings[0].message


def test_an_f_string_tail_with_no_extension_is_unresolvable(
    tmp_path: Path,
) -> None:
    _declare(tmp_path, {"raw_queries": ("parquet", ["QUERY_ID"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


def extract(out_dir: Path, name: str) -> ExtractOutput:
    out_file = out_dir / f"{name}_partitions"
    return ExtractOutput(raw_queries=FileReference.from_local(out_file))
"""
    assert _run(tmp_path, {"app/main.py": src}) == []


def test_a_dotted_directory_in_a_fragment_is_not_an_extension(
    tmp_path: Path,
) -> None:
    """The dot is on a directory, so the file itself has no extension."""
    _declare(tmp_path, {"raw_queries": ("parquet", ["QUERY_ID"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


def extract(out_dir: Path, name: str) -> ExtractOutput:
    out_file = out_dir / f"{name}.d/queries"
    return ExtractOutput(raw_queries=FileReference.from_local(out_file))
"""
    assert _run(tmp_path, {"app/main.py": src}) == []


def test_a_path_variable_assigned_two_extensions_is_unresolvable(
    tmp_path: Path,
) -> None:
    """Ambiguity drops the key rather than picking one of the two answers."""
    _declare(tmp_path, {"raw_queries": ("parquet", ["QUERY_ID"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


def extract(out_dir: Path, streaming: bool) -> ExtractOutput:
    out_file = out_dir / "queries.ndjson"
    if streaming:
        out_file = out_dir / "queries.jsonl"
    return ExtractOutput(raw_queries=FileReference.from_local(out_file))
"""
    assert _run(tmp_path, {"app/main.py": src}) == []


def test_a_reference_built_elsewhere_is_not_followed(tmp_path: Path) -> None:
    """Cross-function plumbing is out of reach, and stays silent rather than guessing."""
    _declare(tmp_path, {"raw_queries": ("parquet", ["QUERY_ID"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    raw_queries: FileReference | None = None


def _write(out_dir: Path) -> FileReference:
    return FileReference(local_path=str(out_dir / "queries.ndjson"))


def extract(out_dir: Path) -> ExtractOutput:
    ref = _write(out_dir)
    return ExtractOutput(raw_queries=ref)
"""
    assert _run(tmp_path, {"app/main.py": src}) == []


def test_an_undeclared_field_is_never_reported(tmp_path: Path) -> None:
    """K016 owns "no declaration"; K017 only grades declarations that exist."""
    _declare(tmp_path, {"raw_queries": ("parquet", ["QUERY_ID"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class ExtractOutput(Output):
    side_channel: FileReference | None = None


def extract(out_dir: Path) -> ExtractOutput:
    out_file = out_dir / "side.ndjson"
    return ExtractOutput(side_channel=FileReference.from_local(out_file))
"""
    assert _run(tmp_path, {"app/main.py": src}) == []


# ---------------------------------------------------------------------------
# Writer fields vs declared fields
# ---------------------------------------------------------------------------


def test_a_record_field_the_declaration_omits_is_reported(tmp_path: Path) -> None:
    _declare(tmp_path, {"transformed_entities": ("ndjson", ["typeName"])})

    findings = _run(
        tmp_path, {"app/main.py": _writer_source(filename="entities.ndjson")}
    )

    assert len(findings) == 1
    message = findings[0].message
    assert "'Entity'" in message
    assert "'qualifiedName'" in message
    assert "# conformance: ignore[K017] <reason>" in message


def test_declared_nested_paths_satisfy_their_top_level_field(tmp_path: Path) -> None:
    """A declaration addressing ``attributes.name`` declares ``attributes``."""
    _write_schemas(
        tmp_path / "app" / "generated" / "artifact_schemas.json",
        {"transformed_entities": ("ndjson", ["typeName"])},
    )
    # Re-write with a nested path so the top-level truncation is what matches.
    path = tmp_path / "app" / "generated" / "artifact_schemas.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schemas"]["transformed_entities"]["fields"].append(
        {
            "name": "attributes.qualifiedName",
            "type": "string",
            "required": True,
            "description": "Stable identity of the entity.",
        }
    )
    path.write_text(json.dumps(data), encoding="utf-8")

    src = _writer_source(
        filename="entities.ndjson",
        record_fields="    typeName: str\n    attributes: dict",
    )
    assert _run(tmp_path, {"app/main.py": src}) == []


def test_a_renaming_record_class_is_skipped(tmp_path: Path) -> None:
    """Wire names are not attribute names, so the comparison cannot be made."""
    _declare(tmp_path, {"transformed_entities": ("ndjson", ["typeName"])})
    src = """
from pathlib import Path

import msgspec

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class Entity(msgspec.Struct, rename="camel"):
    type_name: str
    qualified_name: str


class TransformOutput(Output):
    transformed_entities: FileReference | None = None


def transform(out_dir: Path) -> TransformOutput:
    out_file = out_dir / "entities.ndjson"
    with out_file.open("wb") as handle:
        entity = Entity()
        handle.write(encode(entity))
    return TransformOutput(
        transformed_entities=FileReference(local_path=str(out_file)),
    )
"""
    assert _run(tmp_path, {"app/main.py": src}) == []


def test_two_candidate_record_types_are_unresolvable(tmp_path: Path) -> None:
    _declare(tmp_path, {"transformed_entities": ("ndjson", ["typeName"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class Entity:
    typeName: str
    qualifiedName: str


class Envelope:
    payload: str
    checksum: str


class TransformOutput(Output):
    transformed_entities: FileReference | None = None


def transform(out_dir: Path) -> TransformOutput:
    out_file = out_dir / "entities.ndjson"
    with out_file.open("wb") as handle:
        handle.write(encode(Envelope(payload=Entity())))
    return TransformOutput(
        transformed_entities=FileReference(local_path=str(out_file)),
    )
"""
    assert _run(tmp_path, {"app/main.py": src}) == []


def test_a_record_produced_by_a_mapper_is_invisible(tmp_path: Path) -> None:
    """Only in-repo classes count; a mapper's return value has no resolvable shape."""
    _declare(tmp_path, {"transformed_entities": ("ndjson", ["typeName"])})
    src = """
from pathlib import Path

from app.mappers import map_entity
from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class TransformOutput(Output):
    transformed_entities: FileReference | None = None


def transform(out_dir: Path, rows: list) -> TransformOutput:
    out_file = out_dir / "entities.ndjson"
    with out_file.open("wb") as handle:
        for row in rows:
            handle.write(map_entity(row).to_nested_bytes())
    return TransformOutput(
        transformed_entities=FileReference(local_path=str(out_file)),
    )
"""
    assert _run(tmp_path, {"app/main.py": src}) == []


def test_a_record_class_in_another_module_still_resolves(tmp_path: Path) -> None:
    """The class registry is cross-file even though writer resolution is not."""
    _declare(tmp_path, {"transformed_entities": ("ndjson", ["typeName"])})
    records = """
class Entity:
    typeName: str
    qualifiedName: str
"""
    writer = """
from pathlib import Path

from app.records import Entity
from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class TransformOutput(Output):
    transformed_entities: FileReference | None = None


def transform(out_dir: Path) -> TransformOutput:
    out_file = out_dir / "entities.ndjson"
    with out_file.open("wb") as handle:
        handle.write(encode(Entity()))
    return TransformOutput(
        transformed_entities=FileReference(local_path=str(out_file)),
    )
"""
    findings = _run(tmp_path, {"app/records.py": records, "app/main.py": writer})

    assert len(findings) == 1
    assert "'qualifiedName'" in findings[0].message


def test_inherited_record_fields_are_compared_too(tmp_path: Path) -> None:
    _declare(tmp_path, {"transformed_entities": ("ndjson", ["typeName"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class BaseRecord:
    ingested_at: str


class Entity(BaseRecord):
    typeName: str


class TransformOutput(Output):
    transformed_entities: FileReference | None = None


def transform(out_dir: Path) -> TransformOutput:
    out_file = out_dir / "entities.ndjson"
    with out_file.open("wb") as handle:
        handle.write(encode(Entity()))
    return TransformOutput(
        transformed_entities=FileReference(local_path=str(out_file)),
    )
"""
    findings = _run(tmp_path, {"app/main.py": src})

    assert len(findings) == 1
    assert "'ingested_at'" in findings[0].message


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def test_an_inline_directive_suppresses_the_finding(tmp_path: Path) -> None:
    _declare(tmp_path, {"transformed_entities": ("parquet", ["typeName"])})
    src = """
from pathlib import Path

from application_sdk.contracts.base import Output
from application_sdk.contracts.types import FileReference


class TransformOutput(Output):
    transformed_entities: FileReference | None = None


def transform(out_dir: Path) -> TransformOutput:
    out_file = out_dir / "entities.ndjson"
    return TransformOutput(
        # conformance: ignore[K017] migrating this hand-off to parquet in CONNECT-0.
        transformed_entities=FileReference(local_path=str(out_file)),
    )
"""
    findings = _run(tmp_path, {"app/main.py": src})

    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert findings[0].suppression_justification is not None


# ---------------------------------------------------------------------------
# Repo shapes the check declines to grade
# ---------------------------------------------------------------------------


def test_no_generated_tree_is_silent(tmp_path: Path) -> None:
    assert _run(tmp_path, {"app/main.py": _NDJSON_WRITER}) == []


def test_a_malformed_declarations_file_is_silent(tmp_path: Path) -> None:
    path = tmp_path / "app" / "generated" / "artifact_schemas.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert _run(tmp_path, {"app/main.py": _NDJSON_WRITER}) == []
    assert read_declarations(tmp_path) == {}


def test_an_unknown_declared_format_is_dropped(tmp_path: Path) -> None:
    _declare(tmp_path, {"transformed_entities": ("avro", ["typeName"])})

    assert read_declarations(tmp_path) == {}
    assert _run(tmp_path, {"app/main.py": _NDJSON_WRITER}) == []


def test_a_field_two_entrypoints_declare_differently_is_dropped(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "app" / "generated"
    _write_schemas(
        generated / "extract" / "artifact_schemas.json",
        {"transformed_entities": ("parquet", ["typeName"])},
    )
    _write_schemas(
        generated / "transform" / "artifact_schemas.json",
        {"transformed_entities": ("ndjson", ["typeName"])},
    )

    assert read_declarations(tmp_path) == {}
    assert _run(tmp_path, {"app/main.py": _writer_source(filename="x.jsonl")}) == []


def test_a_field_two_entrypoints_declare_identically_still_answers(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "app" / "generated"
    for entrypoint in ("extract", "transform"):
        _write_schemas(
            generated / entrypoint / "artifact_schemas.json",
            {"transformed_entities": ("parquet", ["typeName", "qualifiedName"])},
        )

    findings = _run(
        tmp_path, {"app/main.py": _writer_source(filename="entities.jsonl")}
    )

    assert len(findings) == 1
    assert "'.jsonl'" in findings[0].message


def test_a_file_that_cannot_be_parsed_is_skipped(tmp_path: Path) -> None:
    _declare(
        tmp_path,
        {"transformed_entities": ("parquet", ["typeName", "qualifiedName"])},
    )

    findings = _run(
        tmp_path,
        {
            "app/broken.py": "def (:\n",
            "app/main.py": _writer_source(filename="entities.jsonl"),
        },
    )

    assert len(findings) == 1
    assert findings[0].file == "app/main.py"
