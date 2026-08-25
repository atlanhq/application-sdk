"""K017 ArtifactSchemaWriterMismatch — check implementation.

Cross-references the artifact schemas an app has already **declared** against
what its Python actually **writes** for the same contract field.  Two
disagreements are reported:

* the writer's file extension cannot be the declared ``format`` — a
  ``.parquet`` path for an ``ndjson`` declaration, or the reverse;
* the record class the writer serialises into the artifact carries a field the
  declaration does not describe.

K016 asks whether a declaration exists at all; K017 assumes one does and asks
whether it is true.  A declaration nobody checks against the writer is a comment
— it reads as an assertion about the file, ages out silently as the writer moves
on, and the consumer that trusted it fails a hop later with no trace back to the
edit that broke it.

Structure follows K006 (``manifest_contract``) and K016
(``artifact_schema_declared``): the same two-pass cross-file class registry,
the same committed-generated-artifact read, and the same conservative policy —
every shape the check cannot resolve contributes nothing rather than a guess.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    make_finding,
)
from conformance.suite.checks._entrypoint_contract_fields import resolve_contract_fields
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
)
from conformance.suite.schema.findings import Finding

from ._declarations import ArtifactDeclaration, read_declarations
from ._writer import ModuleWriters, scan_module

_RULE_ID = "K017"

#: File extension -> the artifact formats a file with that extension can be.
#:
#: ``.json`` maps to ``ndjson`` because NDJSON written to a ``.json`` path is
#: ordinary in this fleet, and ``json`` is not itself a declarable artifact
#: format.  An extension absent from this map — ``.csv``, ``.txt``, a
#: directory, no extension at all — is one the check has no opinion about and
#: is skipped, which is why a partitioned-parquet directory reference never
#: fires this rule.
_EXTENSION_FORMATS: dict[str, frozenset[str]] = {
    ".parquet": frozenset({"parquet"}),
    ".pq": frozenset({"parquet"}),
    ".ndjson": frozenset({"ndjson"}),
    ".jsonl": frozenset({"ndjson"}),
    ".json": frozenset({"ndjson"}),
}

#: How each declarable format is spelled on disk, for the remedy text.
_FORMAT_EXTENSIONS: dict[str, str] = {
    "ndjson": ".ndjson, .jsonl or .json",
    "parquet": ".parquet",
}

#: Keyword arguments that rename a field on the wire.  A record class using any
#: of them serialises under names its Python attributes do not carry, so its
#: attribute list is not the artifact's field list and the field half of this
#: rule must stay out of it.
_RENAMING_KEYWORDS: frozenset[str] = frozenset(
    {
        "rename",
        "alias",
        "alias_generator",
        "serialization_alias",
        "validation_alias",
        "name",
    }
)


def _renames_fields(rec: ClassRecord, by_name: dict[str, ClassRecord]) -> bool:
    """Whether *rec* (or an in-repo ancestor) renames any field on the wire.

    Covers the two idioms in use: a class-level ``rename=`` / ``alias_generator=``
    keyword (msgspec ``Struct``, pydantic ``ConfigDict``) and a per-field
    ``Field(alias=...)`` / ``msgspec.field(name=...)``.  Detection is by keyword
    name rather than by resolving the library, so an unfamiliar model base is
    handled the same way as a familiar one.
    """
    seen: set[str] = set()

    def visit(name: str) -> bool:
        if name in seen:
            return False
        seen.add(name)
        current = by_name.get(name)
        if current is None:
            return False
        for keyword in current.node.keywords:
            if keyword.arg in _RENAMING_KEYWORDS:
                return True
        for node in ast.walk(current.node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            callee = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if callee not in {"Field", "field", "ConfigDict"}:
                continue
            if any(kw.arg in _RENAMING_KEYWORDS for kw in node.keywords):
                return True
        return any(visit(base) for base in current.bases)

    return visit(rec.name)


def _format_message(
    *,
    field_name: str,
    declaration: ArtifactDeclaration,
    suffix: str,
    writer_formats: frozenset[str],
) -> str:
    """Build the finding text for a declared-format vs written-extension clash."""
    writer_format = sorted(writer_formats)[0]
    return (
        f"The '{field_name}' artifact is written to a path ending '{suffix}', but "
        f"'{declaration.source}' declares format = \"{declaration.format}\" for it. "
        f"The declaration is what the SDK validates the artifact against at the "
        f"hand-off and what a consuming app reads to learn the file's shape, so a "
        f"'{declaration.format}' reader handed a '{suffix}' file fails at the "
        f"consumer — one hop away from the edit that caused it, with nothing in "
        f"either app's own tooling connecting the two.\n"
        f"\n"
        f"Fix whichever side is wrong. If the declaration is right, write the file "
        f"as {_FORMAT_EXTENSIONS[declaration.format]}. If the writer is right, "
        f"correct the pkl contract and regenerate:\n"
        f"\n"
        f"    artifactSchemas {{\n"
        f'      ["{field_name}"] = new ArtifactSchema {{\n'
        f'        format = "{writer_format}"\n'
        f"        fields {{ new ArtifactField {{ ... }} }}\n"
        f"      }}\n"
        f"    }}\n"
        f"\n"
        f"Never hand-edit the generated '{declaration.source}' — it is a pkl eval "
        f"output and the next toolkit run reverts the edit. Suppress with "
        f"'# conformance: ignore[K017] <reason>' on the FileReference "
        f"construction."
    )


def _field_message(
    *,
    field_name: str,
    declaration: ArtifactDeclaration,
    record_class: str,
    writer_field: str,
) -> str:
    """Build the finding text for a writer field the declaration does not carry."""
    return (
        f"'{record_class}' is written to the '{field_name}' artifact and declares "
        f"a field '{writer_field}', but '{declaration.source}' does not describe "
        f"'{writer_field}' in that artifact's schema. The declaration is the only "
        f"statement of the file's shape a consuming app can read; a field the "
        f"writer emits and the declaration omits is one the consumer has no reason "
        f"to expect and no way to discover, and it stays that way until something "
        f"downstream needs it.\n"
        f"\n"
        f"Fix whichever side is wrong. If the field belongs in the hand-off, "
        f"declare it in the pkl contract and regenerate:\n"
        f"\n"
        f"    artifactSchemas {{\n"
        f'      ["{field_name}"] = new ArtifactSchema {{\n'
        f'        format = "{declaration.format}"\n'
        f"        fields {{\n"
        f"          new ArtifactField {{\n"
        f'            name = "{writer_field}"\n'
        f'            type = "string"  // the field\'s real logical type\n'
        f'            description = "Why a reader cares about this field."\n'
        f"          }}\n"
        f"        }}\n"
        f"      }}\n"
        f"    }}\n"
        f"\n"
        f"If it does not, drop it from '{record_class}' rather than shipping an "
        f"undocumented column. Never hand-edit the generated "
        f"'{declaration.source}' — it is a pkl eval output and the next toolkit "
        f"run reverts the edit. Suppress with "
        f"'# conformance: ignore[K017] <reason>' on the FileReference "
        f"construction."
    )


def _findings_for_module(
    *,
    rel: str,
    writers: ModuleWriters,
    declared: dict[str, ArtifactDeclaration],
    by_name: dict[str, ClassRecord],
    file_aliases: dict[str, dict[str, str]],
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Diff one module's writer facts against the app's declarations."""
    findings: list[Finding] = []

    for site in writers.sites:
        declaration = declared[site.field_name]

        suffix = writers.suffix_for(site.path_key)
        writer_formats = _EXTENSION_FORMATS.get(suffix or "")
        if writer_formats and declaration.format not in writer_formats:
            findings.append(
                make_finding(
                    filename=rel,
                    rule_id=_RULE_ID,
                    node=site.node,
                    message=_format_message(
                        field_name=site.field_name,
                        declaration=declaration,
                        suffix=suffix or "",
                        writer_formats=writer_formats,
                    ),
                    directives=directives,
                )
            )

        for record_class in sorted(writers.records_for(site.path_key)):
            rec = by_name.get(record_class)
            if rec is None:  # pragma: no cover — candidates come from by_name
                continue
            if _renames_fields(rec, by_name):
                continue
            # A class's own file's aliases, not the scanning file's: a record
            # class may be defined in a different module from its writer.
            aliases = file_aliases.get(rec.file, {})
            fields = resolve_contract_fields(rec.node, aliases, by_name)
            undeclared = sorted(
                f.name for f in fields if f.name not in declaration.top_level_fields
            )
            for writer_field in undeclared:
                findings.append(
                    make_finding(
                        filename=rel,
                        rule_id=_RULE_ID,
                        node=site.node,
                        message=_field_message(
                            field_name=site.field_name,
                            declaration=declaration,
                            record_class=record_class,
                            writer_field=writer_field,
                        ),
                        directives=directives,
                    )
                )

    return findings


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Report writers that disagree with the artifact schema the app declared.

    No-ops when ``app/generated/`` is absent, when no ``artifact_schemas.json``
    declares anything this check understands, and — per declaration — when the
    writer's extension or record type cannot be resolved.  Matches K006's policy
    of preferring a false negative to a false positive on a repo shape the check
    does not understand.
    """
    declared = read_declarations(root)
    if not declared:
        return []
    declared_fields = frozenset(declared)

    # Pass 1: parse every file; build the cross-file class registry (`by_name`)
    # and per-file directives, keyed by repo-relative path. Mirrors K006/K016.
    file_trees: dict[str, ast.Module] = {}
    file_directives: dict[str, dict[int, _IgnoreDirective]] = {}
    file_aliases: dict[str, dict[str, str]] = {}
    by_name: dict[str, ClassRecord] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        if not isinstance(tree, ast.Module):  # pragma: no cover — ast.parse default
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)

        file_trees[rel] = tree
        file_directives[rel] = _parse_directives(text)
        aliases = collect_import_aliases(tree)
        file_aliases[rel] = aliases
        for rec in collect_classes(tree, rel, aliases):
            by_name.setdefault(rec.name, rec)

    # Pass 2: read each module's writer facts and diff them against the
    # declarations. Writer resolution is module-scoped by design — see _writer.
    findings: list[Finding] = []
    for rel, tree in file_trees.items():
        writers = scan_module(tree, declared_fields, by_name)
        if not writers.sites:
            continue
        findings.extend(
            _findings_for_module(
                rel=rel,
                writers=writers,
                declared=declared,
                by_name=by_name,
                file_aliases=file_aliases,
                directives=file_directives[rel],
            )
        )

    return findings
