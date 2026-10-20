"""Recognise a ``warnings.warn(..., DeprecationWarning)`` call in an AST.

A leaf: it imports nothing from ``conformance``. Two rule families read it —
the B-series deprecation extractor and E007's wholesale-deprecated-module skip
(``error_handling._deprecated_module``) — and those packages sit on opposite
sides of an import chain (``_entrypoint_contract_fields`` → ``prescriptions``
→ ``error_handling``, ``deprecation`` → ``_entrypoint_contract_fields``).
When E007 imported this from ``deprecation._extractor`` it closed that chain
into a cycle, and conformance 0.51.0 shipped unable to run
``gen-contract-ledger`` (FND-4176). Keep it importable on its own.
"""

from __future__ import annotations

import ast


def _static_str(node: ast.expr | None) -> str:
    """Best-effort static text of a string expression.

    Plain (and implicitly-concatenated) string literals come through as a single
    ``ast.Constant``.  f-strings parse as ``ast.JoinedStr``; we keep their
    constant parts and drop the interpolated ``{…}`` placeholders — enough to
    detect the migration / removal phrasing.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return ""


def _is_deprecation_warn_call(node: ast.AST) -> str | None:
    """If *node* is ``warnings.warn(<msg>, DeprecationWarning, …)``, return msg.

    Recognises both ``warnings.warn(...)`` and a bare ``warn(...)`` (from
    ``from warnings import warn``).  ``DeprecationWarning`` may be the second
    positional argument or the ``category=`` keyword.  Returns ``None`` when the
    call is not a ``DeprecationWarning`` emission.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    is_warn = (isinstance(func, ast.Attribute) and func.attr == "warn") or (
        isinstance(func, ast.Name) and func.id == "warn"
    )
    if not is_warn:
        return None

    def _names_deprecation(expr: ast.expr | None) -> bool:
        return isinstance(expr, ast.Name) and expr.id == "DeprecationWarning"

    positional = len(node.args) >= 2 and _names_deprecation(node.args[1])
    keyword = any(
        kw.arg == "category" and _names_deprecation(kw.value) for kw in node.keywords
    )
    if not (positional or keyword):
        return None
    return _static_str(node.args[0]) if node.args else ""
