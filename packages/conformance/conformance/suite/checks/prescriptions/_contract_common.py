"""Shared AST helpers for contract-field inspection (P011, P012).

A *contract class* is a class whose base list names ``Input`` or ``Output`` —
the SDK's typed task-boundary contracts.  P011/P012 inspect the annotated
fields of such classes, so the type-annotation recognisers and field-doc
extraction live here, away from any single rule module.
"""

from __future__ import annotations

import ast
from typing import Iterator

_CONTRACT_BASE_NAMES: frozenset[str] = frozenset({"Input", "Output"})


def _base_name(node: ast.expr) -> str | None:
    """Return the terminal name of a base expression (``Input`` / ``mod.Output``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def is_contract_class(node: ast.ClassDef) -> bool:
    """True if any base name is in :data:`_CONTRACT_BASE_NAMES`."""
    return any(_base_name(base) in _CONTRACT_BASE_NAMES for base in node.bases)


def iter_contract_classes(tree: ast.AST) -> Iterator[ast.ClassDef]:
    """Yield every ``ClassDef`` in *tree* that looks like a contract class."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and is_contract_class(node):
            yield node


def _is_name(node: ast.expr, target: str) -> bool:
    return isinstance(node, ast.Name) and node.id == target


def _is_none(node: ast.expr) -> bool:
    # ``None`` parses as ``ast.Constant(value=None)`` on Python 3.8+, but tolerate
    # a bare ``Name("None")`` for robustness.
    return (isinstance(node, ast.Constant) and node.value is None) or _is_name(
        node, "None"
    )


def _is_optional_union(node: ast.expr, target: str) -> bool:
    """True for ``target | None`` (or ``None | target``) PEP 604 unions."""
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.BitOr):
        return False
    left, right = node.left, node.right
    return (_is_name(left, target) and _is_none(right)) or (
        _is_name(right, target) and _is_none(left)
    )


def _is_optional_subscript(node: ast.expr, target: str) -> bool:
    """True for ``Optional[target]``."""
    if not isinstance(node, ast.Subscript):
        return False
    if not _is_name(node.value, "Optional"):
        return False
    return _is_name(node.slice, target)


def _is_annotation(node: ast.expr, target: str) -> bool:
    return (
        _is_name(node, target)
        or _is_optional_union(node, target)
        or _is_optional_subscript(node, target)
    )


def is_bytes_annotation(node: ast.expr) -> bool:
    """True for ``bytes``, ``bytes | None``, ``Optional[bytes]``."""
    return _is_annotation(node, "bytes")


def is_str_annotation(node: ast.expr) -> bool:
    """True for ``str``, ``str | None``, ``Optional[str]``."""
    return _is_annotation(node, "str")


def field_doc_text(classdef: ast.ClassDef, ann_node: ast.AnnAssign) -> str:
    """Return documentation text for a field, or ``""`` if none is present.

    Two sources are recognised:

    * A ``Field(description="...")`` default call on the ``AnnAssign``.
    * A PEP-257 attribute docstring: a bare string ``Expr`` statement
      immediately following the ``AnnAssign`` in the class body.
    """
    # Field(description="...") default.
    value = ann_node.value
    if isinstance(value, ast.Call):
        terminal = value.func
        is_field = (isinstance(terminal, ast.Name) and terminal.id == "Field") or (
            isinstance(terminal, ast.Attribute) and terminal.attr == "Field"
        )
        if is_field:
            for kw in value.keywords:
                if kw.arg == "description" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        return kw.value.value

    # PEP-257 attribute docstring on the immediately-following statement.
    body = classdef.body
    for idx, stmt in enumerate(body):
        if stmt is ann_node:
            nxt = body[idx + 1] if idx + 1 < len(body) else None
            if (
                isinstance(nxt, ast.Expr)
                and isinstance(nxt.value, ast.Constant)
                and isinstance(nxt.value.value, str)
            ):
                return nxt.value.value
            break
    return ""
