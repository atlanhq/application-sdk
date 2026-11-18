"""Recognise a ``warnings.warn(..., DeprecationWarning)`` call in an AST.

A leaf: it imports nothing from ``conformance``. Three readers use it — the
B-series deprecation extractor, the wholesale-deprecated-module skip
(``checks._deprecated_module``, used by E007 and P023), and P023's
deprecated-class skip (:func:`_class_warn_message`) — and those packages sit
on opposite sides of an import chain (``_entrypoint_contract_fields`` → ``prescriptions``
→ ``error_handling``, ``deprecation`` → ``_entrypoint_contract_fields``).
When E007 imported this from ``deprecation._extractor`` it closed that chain
into a cycle, and conformance 0.51.0 shipped unable to run
``gen-contract-ledger`` (FND-4176). Keep it importable on its own.
"""

from __future__ import annotations

import ast
from collections import deque

_WARN_INIT_METHODS: frozenset[str] = frozenset({"__init__", "__init_subclass__"})


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


#: The SDK's notice helper (``application_sdk._deprecation.warn_deprecated``).
WARN_DEPRECATED_NAME = "warn_deprecated"
_WARN_DEPRECATED_PARAMS = ("name", "replacement", "remove_in")


def _call_arg(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((kw.value for kw in call.keywords if kw.arg == keyword), None)


def warn_deprecated_message(node: ast.AST) -> str | None:
    """If *node* is a ``warn_deprecated(...)`` call, return the notice it emits.

    The helper formats ``name``, ``replacement``, ``remove_in`` and ``note``
    into one fixed sentence, so the notice is rebuilt here from the literal
    arguments in that same format (``application_sdk._deprecation.
    deprecation_message``). An argument that is not a literal comes back
    empty, which leaves the removal version unreadable, as it would be for a
    ``warnings.warn`` given a variable.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    called = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else None
    )
    if called != WARN_DEPRECATED_NAME:
        return None
    name, replacement, remove_in = (
        _static_str(_call_arg(node, i, kw))
        for i, kw in enumerate(_WARN_DEPRECATED_PARAMS)
    )
    note_arg = next((kw.value for kw in node.keywords if kw.arg == "note"), None)
    note = _static_str(note_arg).rstrip(" .")
    why = f" — {note}" if note else ""
    version = f"v{remove_in}" if remove_in else ""
    return (
        f"{name} is deprecated; use {replacement} instead{why}. "
        f"Will be removed in {version}."
    )


def _is_deprecation_warn_call(node: ast.AST) -> str | None:
    """If *node* emits a ``DeprecationWarning``, return its message.

    Recognises ``warnings.warn(<msg>, DeprecationWarning, …)``, a bare
    ``warn(...)`` (from ``from warnings import warn``), and the SDK's
    ``warn_deprecated(...)`` helper.  ``DeprecationWarning`` may be the second
    positional argument or the ``category=`` keyword.  Returns ``None`` when the
    call is not a ``DeprecationWarning`` emission.
    """
    if not isinstance(node, ast.Call):
        return None
    helper_message = warn_deprecated_message(node)
    if helper_message is not None:
        return helper_message
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


def _class_warn_message(class_node: ast.ClassDef) -> str | None:
    """Return a class's deprecation message if ``__init__`` / ``__init_subclass__``
    directly emits a ``DeprecationWarning``, else ``None``.

    Walks each such method's executable body (so a ``warn`` nested in a guard
    ``if`` still counts) but does **not** descend into a nested ``def``,
    ``lambda`` or ``class``, nor into other methods — a warn there does not run
    at construction, and the warn must belong to construction/subclassing of
    *this* class for the attribution to be sound.
    """
    for item in class_node.body:
        if (
            isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            and item.name in _WARN_INIT_METHODS
        ):
            for sub in _walk_own_scope(item):
                message = _is_deprecation_warn_call(sub)
                if message is not None:
                    return message
    return None


def _walk_own_scope(function: ast.FunctionDef | ast.AsyncFunctionDef):
    """``ast.walk`` over *function*'s body that stops at nested scopes.

    Breadth-first, in source order, as ``ast.walk`` is: the first warn found
    is the message the manifest records, so the order is part of the output.
    """
    pending: deque[ast.AST] = deque(function.body)
    while pending:
        node = pending.popleft()
        yield node
        if not isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
        ):
            pending.extend(ast.iter_child_nodes(node))
