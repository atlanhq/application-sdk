"""P011/P012 — payload-unsafe field types on ``Input``/``Output`` contracts.

* ``P011`` flags ``bytes`` fields: raw bytes embedded in a contract ride the
  Temporal payload and hit its 2 MB limit.
* ``P012`` flags ``str`` fields that look like filesystem paths: a bare path is
  a worker-local reference that is invalid on a different worker.

Both should instead carry a ``FileReference`` field.
"""

from __future__ import annotations

import ast
import re

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._contract_common import (
    field_doc_text,
    is_bytes_annotation,
    is_str_annotation,
    iter_contract_classes,
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
    """Emit P011 for ``bytes`` fields on contract classes."""
    findings: list[Finding] = []
    for classdef in iter_contract_classes(tree):
        for name, ann_node in _iter_field_annassigns(classdef):
            if is_bytes_annotation(ann_node.annotation):
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P011",
                        node=ann_node,
                        message=(
                            f"Contract field '{name}: bytes' embeds raw bytes — "
                            "large payloads hit Temporal's 2 MB limit. Use a "
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
    for classdef in iter_contract_classes(tree):
        for name, ann_node in _iter_field_annassigns(classdef):
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
