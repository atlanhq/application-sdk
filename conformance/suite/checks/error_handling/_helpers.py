"""Shared AST helper functions used across E-series rule modules."""

from __future__ import annotations

import ast
from collections.abc import Iterator

from ._constants import (
    _BROAD_EXCEPT_TYPES,
    _LOG_METHODS,
    BUILTIN_RAISES,
)


def _get_name(node: ast.expr | ast.AST | None) -> str | None:
    """Extract a simple name string from a Name or Attribute node."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _raise_exc_name(exc: ast.expr) -> str | None:
    """Get the exception class name from a raise-expression.

    Handles both ``raise ValueError`` (Name) and ``raise ValueError(...)``
    (Call wrapping a Name or Attribute).
    """
    if isinstance(exc, ast.Call):
        return _get_name(exc.func)
    return _get_name(exc)


def _is_log_call_stmt(stmt: ast.stmt) -> bool:
    """True if *stmt* is a bare ``logger.<method>(...)`` expression."""
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if isinstance(call, ast.Await):
        call = call.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    return isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS


def _any_logging_in(stmts: list[ast.stmt]) -> bool:
    """True if any logging call appears anywhere within *stmts*."""
    for stmt in stmts:
        if _is_log_call_stmt(stmt):
            return True
        for node in _iter_shallow(stmt):
            if isinstance(node, ast.stmt) and _is_log_call_stmt(node):
                return True
    return False


def _has_exc_info(call: ast.Call) -> bool:
    """True if *call* has ``exc_info=True`` among its keywords."""
    for kw in call.keywords:
        if kw.arg == "exc_info":
            val = kw.value
            if isinstance(val, ast.Constant) and val.value is True:
                return True
    return False


def _body_is_only_pass(stmts: list[ast.stmt]) -> bool:
    """True if the body is exclusively Pass (ignoring docstring constants)."""
    real = [
        s
        for s in stmts
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) == 1 and isinstance(real[0], ast.Pass)


def _body_is_only_loop_control_no_logging(stmts: list[ast.stmt]) -> bool:
    """True if body only has continue/break/pass with no logging."""
    if _any_logging_in(stmts):
        return False
    real = [
        s
        for s in stmts
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) > 0 and all(
        isinstance(s, (ast.Continue, ast.Break, ast.Pass)) for s in real
    )


def _is_gather_call(call: ast.Call) -> bool:
    """True if *call* is ``asyncio.gather(...)`` or bare ``gather(...)``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "gather"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        )
    if isinstance(func, ast.Name):
        return func.id == "gather"
    return False


def is_broad_suppress(node: ast.Call) -> bool:
    """True if *node* is ``contextlib.suppress(Exception|BaseException)``."""
    name = _get_name(node.func)
    if name != "suppress":
        return False
    for arg in node.args:
        if _get_name(arg) in _BROAD_EXCEPT_TYPES:
            return True
    return False


def is_builtin_raise(raise_node: ast.Raise) -> bool:
    """True if this raise targets a name in BUILTIN_RAISES."""
    if raise_node.exc is None:
        return False
    return _raise_exc_name(raise_node.exc) in BUILTIN_RAISES


def _get_decorator_names(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    names: set[str] = set()
    for dec in func.decorator_list:
        # @decorator → Name; @decorator("arg") → Call whose func is Name/Attribute
        if isinstance(dec, ast.Call):
            n = _get_name(dec.func)
        else:
            n = _get_name(dec)
        if n:
            names.add(n)
    return frozenset(names)


def _inherits_logging_filter(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        n = _get_name(base)
        if n == "Filter":
            return True
    return False


def _find_filter_method(
    cls: ast.ClassDef,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for item in cls.body:
        if (
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "filter"
        ):
            return item
    return None


def _filter_body_wrapped(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the filter() body is a single Try node (fully wrapped)."""
    real = [
        s
        for s in method.body
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) == 1 and isinstance(real[0], ast.Try)


def _message_kw_has_exc_text(kw_value: ast.expr, exc_binding: str | None) -> bool:
    """True if a ``message=`` value embeds caught-exception text."""
    if exc_binding is None:
        return False
    # f-string containing the exception binding name
    if isinstance(kw_value, ast.JoinedStr):
        for part in ast.walk(kw_value):
            if isinstance(part, ast.FormattedValue):
                inner = part.value
                if isinstance(inner, ast.Name) and inner.id == exc_binding:
                    return True
                if (
                    isinstance(inner, ast.Call)
                    and _get_name(inner.func) in ("str", "repr")
                    and inner.args
                    and isinstance(inner.args[0], ast.Name)
                    and inner.args[0].id == exc_binding
                ):
                    return True
        return False
    # str(exc) / repr(exc) directly
    if isinstance(kw_value, ast.Call) and (
        _get_name(kw_value.func) in ("str", "repr")
        and kw_value.args
        and (
            isinstance(kw_value.args[0], ast.Name)
            and kw_value.args[0].id == exc_binding
        )
    ):
        return True
    # BinOp concat referencing the binding
    if isinstance(kw_value, ast.BinOp) and isinstance(kw_value.op, ast.Add):
        for node in ast.walk(kw_value):
            if isinstance(node, ast.Name) and node.id == exc_binding:
                return True
    return False


def _iter_shallow(root: ast.AST) -> Iterator[ast.AST]:
    """Yield descendants of *root* without crossing nested function/class defs."""
    queue = list(ast.iter_child_nodes(root))
    while queue:
        node = queue.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            queue.extend(ast.iter_child_nodes(node))
