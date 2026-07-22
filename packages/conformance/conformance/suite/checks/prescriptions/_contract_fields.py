"""P011 RawBytesInContract / P012 FilePathStringInContract — payload-unsafe field types.

* ``P011`` RawBytesInContract — flags ``bytes``, ``bytearray``, and
  ``memoryview`` fields: raw binary data embedded in a contract rides the
  Temporal payload and hits its 2 MB limit.
* ``P012`` FilePathStringInContract — flags ``str`` fields that look like
  filesystem paths: a bare path is a worker-local reference that is invalid on
  a different worker.

Both should instead carry a ``FileReference`` field.

Only classes that extend ``Input``/``Output`` imported from ``application_sdk.*``
are inspected; third-party types with the same terminal name are excluded via
import-provenance tracking (see ``collect_foreign_contract_names``).
"""

from __future__ import annotations

import ast
import re

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._contract_common import (
    bytes_type_name,
    collect_foreign_contract_names,
    field_doc_text,
    is_bytes_annotation,
    is_str_annotation,
    is_workflow_path_annotation,
    iter_contract_classes,
    unmodeled_container_name,
)

_PATH_NAME_RE = re.compile(
    r"(^|_)(path|paths|dir|dirs|directory|directories|file|files|filepath|filename|folder)($|_)"
)

_PATH_DOC_SIGNALS: tuple[str, ...] = (
    "path to",
    "file path",
    "local path",
    "directory",
    "folder",
    ".json",
    ".parquet",
    ".csv",
    ".jsonl",
)


def _iter_field_annassigns(classdef: ast.ClassDef):
    """Yield ``(name, AnnAssign)`` for each annotated field at class-body level."""
    for stmt in classdef.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            yield stmt.target.id, stmt


def check_p011(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P011 for bytes-like fields on contract classes."""
    findings: list[Finding] = []
    foreign = collect_foreign_contract_names(tree)
    for classdef in iter_contract_classes(tree, foreign):
        for name, ann_node in _iter_field_annassigns(classdef):
            if not is_bytes_annotation(ann_node.annotation):
                continue
            matched_type = bytes_type_name(ann_node.annotation) or "bytes"
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P011",
                    node=ann_node,
                    message=(
                        f"Contract field '{name}: {matched_type}' embeds raw binary "
                        "data — large payloads hit Temporal's 2 MB limit. Use a "
                        "FileReference field to store the data as a file and "
                        "pass the ref instead."
                    ),
                    directives=directives,
                )
            )
    return findings


def _looks_like_path(name: str, doc: str) -> bool:
    if _PATH_NAME_RE.search(name):
        return True
    doc_lower = doc.lower()
    return any(signal in doc_lower for signal in _PATH_DOC_SIGNALS)


def check_p012(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P012 for ``str`` fields that look like filesystem paths."""
    findings: list[Finding] = []
    foreign = collect_foreign_contract_names(tree)
    for classdef in iter_contract_classes(tree, foreign):
        for name, ann_node in _iter_field_annassigns(classdef):
            # WorkflowPath is the sanctioned str type for deterministic,
            # worker-portable workflow-relative paths — exempt from P012.
            if is_workflow_path_annotation(ann_node.annotation):
                continue
            if not is_str_annotation(ann_node.annotation):
                continue
            doc = field_doc_text(classdef, ann_node)
            if not _looks_like_path(name, doc):
                continue
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P012",
                    node=ann_node,
                    message=(
                        f"Contract field '{name}: str' appears to be a "
                        "file/directory path — a bare string is a local-filesystem "
                        "reference that is invalid on a different worker. Use a "
                        "FileReference field instead. If this is genuinely a string "
                        "(e.g. a URL), suppress with "
                        "'# conformance: ignore[P012] <reason>'."
                    ),
                    directives=directives,
                )
            )
    return findings


def check_p015(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P015 for unmodeled-container fields on contract classes.

    Flags ``dict``/``list``/``set``-family fields (and their :mod:`typing`
    equivalents) whose element/value type is a primitive or ``Any`` —
    including the bounded ``Annotated[dict[str, str], MaxItems(N)]`` form
    that passes payload-safety validation (P001) but is still stringly-typed.

    Containers of a typed class (``list[FooModel]``, ``dict[str, FooModel]``)
    are exempt: a collection of models is the canonical bounded pattern.
    """
    findings: list[Finding] = []
    foreign = collect_foreign_contract_names(tree)
    for classdef in iter_contract_classes(tree, foreign):
        for name, ann_node in _iter_field_annassigns(classdef):
            container = unmodeled_container_name(ann_node.annotation)
            if container is None:
                continue
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P015",
                    node=ann_node,
                    message=(
                        f"Contract field '{name}' uses an unmodeled container type "
                        f"('{container}' of primitives/Any). Even bounded forms like "
                        f"Annotated[{container}[...], MaxItems(N)] pass payload safety "
                        "but remain stringly-typed — the SDK contract guidance is to "
                        "replace them with a typed nested model (a class inheriting "
                        "from pydantic.BaseModel). Containers of a typed class "
                        f"(e.g. {container}[FooModel]) are already exempt. Suppress "
                        "with '# conformance: ignore[P015] <reason>' when a typed "
                        "replacement is not feasible."
                    ),
                    directives=directives,
                )
            )
    return findings
