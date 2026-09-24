"""F021 PreflightFixedLeafInBroadExcept (CONNECT-1358).

Flags a broad ``except`` inside ``preflight_check`` (or a helper it reaches)
that builds an Auth- or Permission-rooted error without looking at what it
caught. Every failure in the ``try`` then reaches the customer as a credential
or grant problem: an empty credential, a DNS failure and a 500 all read as
"grant access", and the ticket chases source-side grants that were never
missing.

The caught exception, or a name derived from it (``code = exc.status``),
counts as looked at when one of these is on the leaf's own path:

* an enclosing ``if`` / ``while`` / conditional expression / ``match`` that
  tests it (``isinstance(exc, ...)``, ``is_privilege_error(exc)``) with the
  leaf in the branch the test selects. The ``else`` is the fallback for
  everything the test did not match, so
  ``exc if isinstance(exc, AppError) else AuthError(...)`` still fires;
* an enclosing call that receives it (a classifier taking the leaf as its
  default);
* a strictly earlier statement that stores the result of a call receiving it
  (``leaf = classify_http_exception(exc)``, including a walrus in an ``if``).

A dropped result does not count (a bare ``reraise_if_transient(exc)``, a
``logger.debug(..., safe_traceback(exc))``), nor does a call inside the leaf's
own arguments or in a branch that does not enclose the leaf: whatever those
let through still gets the fixed leaf. A nested ``except`` is judged on its
own and never makes its outer handler fire.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry, iter_function_nodes, reachable_preflight_sites
from ._contracts import _Checker, _qualified

_F021 = "F021"

_BROAD = frozenset({"Exception", "BaseException"})
_CUSTOMER_ROOTS = frozenset({"AuthError", "AppPermissionDeniedError"})
_NOT_CLASSIFIERS = frozenset({"str", "repr", "format", "type", "print"})
_CONDITIONS = (ast.If, ast.IfExp, ast.While)
_STORES = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)
_BLOCKS = frozenset({"body", "orelse", "handlers", "finalbody", "cases"})
_OWN_SCOPE_ENDS = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.ExceptHandler,
)


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _OWN_SCOPE_ENDS):
            continue
        yield child
        yield from _own_nodes(child)


def _is_broad(src, handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_qualified(src, kind).rsplit(".", 1)[-1] in _BROAD for kind in kinds)


def _mentions(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(node))


def _receives(call: ast.Call, names: set[str]) -> bool:
    if isinstance(call.func, ast.Name) and call.func.id in _NOT_CLASSIFIERS:
        return False
    values = [*call.args, *(kw.value for kw in call.keywords if kw.arg != "cause")]
    return any(isinstance(v, ast.Name) and v.id in names for v in values)


def _customer_leaf(checker: _Checker, src, call: ast.Call) -> bool:
    return any(
        n.startswith("application_sdk.errors.")
        and n.rsplit(".", 1)[-1] in _CUSTOMER_ROOTS
        for n in checker.error_names(src, call.func)
    )


def _derived_names(handler: ast.ExceptHandler, caught: str) -> set[str]:
    names = {caught}
    for node in _own_nodes(handler):
        if not isinstance(node, _STORES) or node.value is None:
            continue
        if not _mentions(node.value, {caught}):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    return names


def _stores_a_classification(stmt: ast.stmt, names: set[str]) -> bool:
    headers = [
        value
        for field, value in ast.iter_fields(stmt)
        if field not in _BLOCKS and isinstance(value, ast.AST)
    ]
    for node in [stmt, *(n for header in headers for n in ast.walk(header))]:
        if not isinstance(node, _STORES) or node.value is None:
            continue
        if any(
            isinstance(call, ast.Call) and _receives(call, names)
            for call in ast.walk(node.value)
        ):
            return True
    return False


def _parents(handler: ast.ExceptHandler) -> dict[ast.AST, tuple[ast.AST, str]]:
    parents: dict[ast.AST, tuple[ast.AST, str]] = {}
    for parent in [handler, *_own_nodes(handler)]:
        for field, value in ast.iter_fields(parent):
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.AST):
                    parents[child] = (parent, field)
    return parents


def _looked_at(
    leaf: ast.Call,
    names: set[str],
    parents: dict[ast.AST, tuple[ast.AST, str]],
    handler: ast.ExceptHandler,
) -> bool:
    child: ast.AST = leaf
    while child is not handler:
        parent, field = parents[child]
        if (
            isinstance(parent, _CONDITIONS)
            and field == "body"
            and _mentions(parent.test, names)
        ):
            return True
        if isinstance(parent, ast.Match) and _mentions(parent.subject, names):
            return True
        if isinstance(parent, ast.Call) and _receives(parent, names):
            return True
        block = getattr(parent, field)
        if isinstance(block, list) and field in _BLOCKS:
            earlier = block[: block.index(child)]
            if any(_stores_a_classification(stmt, names) for stmt in earlier):
                return True
        child = parent
    return False


def _fixed_leaf(checker: _Checker, src, handler: ast.ExceptHandler) -> ast.Call | None:
    names = _derived_names(handler, handler.name) if handler.name else set()
    parents = _parents(handler)
    for node in _own_nodes(handler):
        if not (isinstance(node, ast.Call) and _customer_leaf(checker, src, node)):
            continue
        if not names or not _looked_at(node, names, parents, handler):
            return node
    return None


def scan(reg: Registry) -> list[Finding]:
    checker = _Checker(reg)
    findings: list[Finding] = []
    for src, method in reachable_preflight_sites(reg, follow_callbacks=True):
        for handler in iter_function_nodes(method):
            if not (isinstance(handler, ast.ExceptHandler) and _is_broad(src, handler)):
                continue
            leaf = _fixed_leaf(checker, src, handler)
            if leaf is None:
                continue
            findings.append(
                make_finding(
                    filename=src.rel,
                    rule_id=_F021,
                    node=handler,
                    message=(
                        f"Broad except in preflight_check builds a fixed "
                        f"{ast.unparse(leaf.func)} for every failure it catches, "
                        "so an empty credential or an unreachable host reads as "
                        "a customer credential or grant problem. Classify the "
                        "caught exception first (application_sdk.errors."
                        "classify_http_exception for httpx, or an isinstance "
                        "chain) and fall back to a leaf that does not blame the "
                        "customer (InternalError when the cause is unknown), or "
                        "narrow the except clause."
                    ),
                    directives=src.directives,
                )
            )
    return findings
