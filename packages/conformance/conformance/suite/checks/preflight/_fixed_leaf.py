"""F021 PreflightFixedLeafInBroadExcept (CONNECT-1358).

Flags a broad ``except`` inside ``preflight_check`` (or a helper it reaches)
that builds an Auth- or Permission-rooted error without looking at what it
caught. Every failure in the ``try`` then reaches the customer as a credential
or grant problem: an empty credential, a DNS failure and a 500 all read as
"grant access", and the ticket chases source-side grants that were never
missing. The SDK's own subclasses outside ``application_sdk.errors``
(``SqlClientAuthFailedError``, ``CredentialError``, ...) count too; a test pins
``_SDK_CUSTOMER_LEAVES`` to the SDK source so a new one cannot be missed.

The leaf is exempt only when the caught exception selects it: an enclosing
``if`` / ``while`` / conditional expression / ``match`` whose test reads the
exception, or a name stored from it earlier (directly or through other stored
names), with the leaf in the branch the test selects. A name counts only if
its value depends on the exception on every path to that test: stores are
replayed in source order up to the test, and one on a branch that may be
skipped can clear a name but never derive it.

Everything else is a fallback and fires:

* the ``else`` and a catch-all ``case _:``, so
  ``exc if isinstance(exc, AppError) else AuthError(...)`` fires;
* a test every caught exception passes, written inline or stored first
  (``if exc:``, ``ready = exc is not None``, ``isinstance(exc, Exception)``);
* a stored result the leaf does not depend on (``detail = redact(exc)``, or
  ``leaf = classify(exc)`` followed by a fixed ``AuthError(...)``), and the
  fallthrough after ``if transient := is_transient(exc): raise transient``;
* a customer-blaming default handed to a classifier,
  ``classify(exc, AuthError(...))``: it is what every unknown cause gets.

A nested ``except`` is judged on its own and never makes its outer handler
fire. Known limit: a negated guard (``if not isinstance(exc, X):
AuthError(...)``) reads as a selected branch, not a fallback.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry, Source, iter_function_nodes, reachable_preflight_sites
from ._contracts import _Checker, _qualified

_F021 = "F021"

_BROAD = frozenset({"Exception", "BaseException"})
_CUSTOMER_ROOTS = frozenset({"AuthError", "AppPermissionDeniedError"})
_SDK_CUSTOMER_LEAVES = frozenset(
    {
        "AwsAssumeRoleError",
        "AwsRdsTokenError",
        "AzureClientAuthError",
        "AzureCredentialError",
        "CredentialError",
        "CredentialNotFoundError",
        "CredentialParseError",
        "CredentialValidationError",
        "OAuthTokenError",
        "SqlAwsCredentialsError",
        "SqlClientAuthFailedError",
        "StoragePermissionError",
    }
)
_CONDITIONS = (ast.If, ast.IfExp, ast.While)
_STORES = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)
_BLOCK_FIELDS = frozenset({"body", "orelse", "finalbody", "cases"})
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


def _is_broad(src: Source, handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_qualified(src, kind).rsplit(".", 1)[-1] in _BROAD for kind in kinds)


def _mentions(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(node))


def _customer_rooted(
    checker: _Checker, src: Source, node: ast.AST, seen: frozenset[int] = frozenset()
) -> bool:
    name = _qualified(src, node)
    if name.startswith("application_sdk."):
        return name.rsplit(".", 1)[-1] in _CUSTOMER_ROOTS | _SDK_CUSTOMER_LEAVES
    resolved = checker.symbol(src, node)
    if resolved is None:
        return False
    owner, cls = resolved
    if not isinstance(cls, ast.ClassDef) or id(cls) in seen:
        return False
    return any(
        _customer_rooted(checker, owner, base, seen | {id(cls)}) for base in cls.bases
    )


def _position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _end(node: ast.AST) -> tuple[int, int]:
    return (
        getattr(node, "end_lineno", 0) or 0,
        getattr(node, "end_col_offset", 0) or 0,
    )


_Parents = dict[ast.AST, tuple[ast.AST, str, int]]
_Step = tuple[int, str, int]


def _parents(handler: ast.ExceptHandler) -> _Parents:
    """Map each node to its parent, the parent's field, and its index in it."""
    parents: _Parents = {}
    for parent in [handler, *_own_nodes(handler)]:
        for field, value in ast.iter_fields(parent):
            values = value if isinstance(value, list) else [value]
            children: list[ast.AST] = [c for c in values if isinstance(c, ast.AST)]
            for index, child in enumerate(children):
                parents[child] = (parent, field, index)
    return parents


def _block_path(
    node: ast.AST, parents: _Parents, handler: ast.ExceptHandler
) -> list[_Step]:
    """The statement-list slots from ``handler`` down to the statement holding ``node``."""
    path: list[_Step] = []
    child = node
    while child is not handler:
        parent, field, index = parents[child]
        if field in _BLOCK_FIELDS:
            path.append((id(parent), field, index))
        child = parent
    path.reverse()
    return path


def _dominates(store: list[_Step], point: list[_Step]) -> bool:
    """Whether a store at ``store`` runs on every path that reaches ``point``.

    True for an earlier statement in the same or an enclosing block, and for
    the header of an enclosing statement (a walrus in an ``if`` test); false
    for anything inside a branch the point is not in.
    """
    if not store or len(store) > len(point):
        return False
    *outer, last = store
    here = point[len(outer)]
    return point[: len(outer)] == outer and here[:2] == last[:2] and last[2] <= here[2]


def _can_reach(store: list[_Step], point: list[_Step], parents: _Parents) -> bool:
    """Whether a store can run on a path that reaches ``point``.

    Stores in the other arm of an if or match cannot change the value seen at
    the point. Earlier conditional stores in its path can reach it, but do not
    necessarily dominate it.
    """
    for left, right in zip(store, point):
        if left[0] != right[0]:
            continue
        parent = next((node for node in parents if id(node) == left[0]), None)
        if left[1] != right[1] and {left[1], right[1]} <= {"body", "orelse"}:
            return False
        if isinstance(parent, ast.Match) and left[1] == right[1] == "cases":
            if left[2] != right[2]:
                return False
    return True


def _executes_unconditionally(node: ast.AST, parents: _Parents) -> bool:
    """Whether expression-level short circuiting can skip this store."""
    child = node
    while child in parents:
        parent, field, index = parents[child]
        if isinstance(parent, ast.BoolOp) and field == "values" and index > 0:
            return False
        if isinstance(parent, ast.IfExp) and field in {"body", "orelse"}:
            return False
        child = parent
    return True


def _derived_at(
    point: ast.expr, caught: str, parents: _Parents, handler: ast.ExceptHandler
) -> set[str]:
    """Names certainly holding a value that depends on ``caught`` when ``point`` runs.

    Stores are replayed in source order. Only a store that dominates the point
    can derive a name; one on a branch that may be skipped can only clear it,
    as can a store from anything else or from a guard every exception passes
    (``exc is not None``).
    """
    point_path = _block_path(point, parents, handler)
    stores = sorted(
        (
            node
            for node in _own_nodes(handler)
            if isinstance(node, _STORES)
            and node.value is not None
            and _end(node) <= _position(point)
        ),
        key=_position,
    )
    names = {caught}
    for node in stores:
        if node.value is None:
            continue
        store_path = _block_path(node, parents, handler)
        if not _can_reach(store_path, point_path, parents):
            continue
        derived = (
            _dominates(store_path, point_path)
            and _executes_unconditionally(node, parents)
            and _mentions(node.value, names)
            and not _always_true(node.value, caught)
        )
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for name in ast.walk(target):
                if not isinstance(name, ast.Name):
                    continue
                if derived:
                    names.add(name.id)
                elif not isinstance(node, ast.AugAssign):
                    names.discard(name.id)
    return names


def _always_true(test: ast.expr, caught: str) -> bool:
    """A guard every exception inside ``except ... as caught`` passes."""
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test = test.operand
    if isinstance(test, ast.Name):
        return test.id == caught
    if isinstance(test, ast.Compare):
        return (
            isinstance(test.left, ast.Name)
            and test.left.id == caught
            and len(test.ops) == 1
            and isinstance(test.ops[0], (ast.Is, ast.IsNot))
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        )
    if isinstance(test, ast.Call) and isinstance(test.func, ast.Name):
        if test.func.id != "isinstance" or len(test.args) != 2:
            return False
        subject, kinds = test.args
        types = kinds.elts if isinstance(kinds, ast.Tuple) else [kinds]
        return (
            isinstance(subject, ast.Name)
            and subject.id == caught
            and all(ast.unparse(t).rsplit(".", 1)[-1] in _BROAD for t in types)
        )
    return False


def _catch_all(case: ast.AST) -> bool:
    return (
        isinstance(case, ast.match_case)
        and case.guard is None
        and isinstance(case.pattern, ast.MatchAs)
        and case.pattern.pattern is None
    )


def _looked_at(
    leaf: ast.Call,
    caught: str,
    parents: _Parents,
    handler: ast.ExceptHandler,
) -> bool:
    child: ast.AST = leaf
    while child is not handler:
        parent, field, _ = parents[child]
        if isinstance(parent, _CONDITIONS) and field == "body":
            names = _derived_at(parent.test, caught, parents, handler)
            if _mentions(parent.test, names) and not _always_true(parent.test, caught):
                return True
        if isinstance(parent, ast.Match) and not _catch_all(child):
            names = _derived_at(parent.subject, caught, parents, handler)
            if _mentions(parent.subject, names):
                return True
        child = parent
    return False


def _fixed_leaf(
    checker: _Checker, src: Source, handler: ast.ExceptHandler
) -> ast.Call | None:
    parents = _parents(handler)
    for node in _own_nodes(handler):
        if not (
            isinstance(node, ast.Call) and _customer_rooted(checker, src, node.func)
        ):
            continue
        if handler.name is None or not _looked_at(node, handler.name, parents, handler):
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
