"""F021 PreflightFixedLeafInBroadExcept (CONNECT-1358).

Flags a broad ``except`` inside ``preflight_check`` (or a helper it reaches
through direct calls, at any depth) that reports an Auth- or Permission-rooted
error with nothing on the way to it that could pick a different one. Every failure in the ``try`` then
reaches the customer as a credential or grant problem, and a hard-mode gate
blocks the run on it: an empty credential, a DNS failure and a 500 all read as
"grant access". The SDK's own subclasses outside ``application_sdk.errors``
(``SqlClientAuthFailedError``, ``CredentialError``, ...) count too; a test pins
``_SDK_CUSTOMER_LEAVES`` to the SDK source.

The rule judges the shape, not the guard. Whether a condition selects the
caught exception is a dataflow question with no bounded answer, so any
condition makes the leaf unknown, and unknown is never reported. A leaf is
reported only when it is raised, or is the ``error=`` of a ``PreflightCheck`` /
``PreflightOutput`` (directly or through ``.to_failure_details()``), and every
step from the ``except`` to that site always runs: statement blocks, ``with``
bodies, ``try ... finally``, and around a row only lists, tuples,
``return``, another row, or ``.append`` / ``.extend`` / ``.insert``. An
``if``, loop, ``match``, conditional or boolean expression around the leaf, a
``try`` body with its own handlers or its ``else``, or an earlier statement in
an enclosing block that can leave it (``raise``, ``return``, ``break``,
``continue``, ``assert``) all make the leaf unknown. So ``if self.strict:``
around the leaf is not reported either, and a clean F021 does not prove the
classification is right. A helper only passed as a callback is not followed.

Known gaps: nothing after the site is read, so a ``finally``, an enclosing
``try`` or a context manager (``contextlib.suppress``, an error translator)
that replaces or swallows the raise, or a later ``raise`` that discards an
appended row, still leaves the leaf reported.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry, Source, iter_function_nodes, reachable_preflight_sites
from ._contracts import _Checker, _qualified, _sdk

_F021 = "F021"

_BROAD = frozenset(
    {"Exception", "BaseException", "builtins.Exception", "builtins.BaseException"}
)
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
_EXITS = (ast.Raise, ast.Return, ast.Continue, ast.Break, ast.Assert)
_NEW_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_OWN_SCOPE_ENDS = (*_NEW_SCOPES, ast.ExceptHandler)
_ROWS = ("PreflightCheck", "PreflightOutput")
_COLLECTORS = frozenset({"append", "extend", "insert"})
_BLOCKS = frozenset({"body", "finalbody"})
_ALWAYS_RUNS: dict[type[ast.AST], tuple[str, ...]] = {
    ast.ExceptHandler: ("body",),
    ast.With: ("body",),
    ast.AsyncWith: ("body",),
    ast.Try: ("body", "finalbody"),
    ast.TryStar: ("body", "finalbody"),
    ast.Call: ("args", "keywords"),
    ast.keyword: ("value",),
    ast.List: ("elts",),
    ast.Tuple: ("elts",),
    ast.Return: ("value",),
    ast.Expr: ("value",),
}

_Parents = dict[ast.AST, tuple[ast.AST, str, int]]


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
    return any(_qualified(src, kind) in _BROAD for kind in kinds)


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


def _ancestry(
    node: ast.AST, parents: _Parents, handler: ast.ExceptHandler
) -> list[tuple[ast.AST, str, int]]:
    """``(parent, field, index)`` from ``node`` up to the handler."""
    chain: list[tuple[ast.AST, str, int]] = []
    child = node
    while child is not handler:
        parent, field, index = parents[child]
        chain.append((parent, field, index))
        child = parent
    return chain


def _block(parent: ast.AST, field: str) -> list[ast.stmt]:
    value = getattr(parent, field, None)
    if not isinstance(value, list):
        return []
    return [s for s in value if isinstance(s, ast.stmt)]


def _can_exit(stmt: ast.stmt) -> bool:
    """Whether running ``stmt`` can leave the enclosing block early."""
    pending: list[ast.AST] = [stmt]
    while pending:
        node = pending.pop()
        if isinstance(node, _EXITS):
            return True
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _NEW_SCOPES):
                pending.append(child)
    return False


def _adds_to_collection(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _COLLECTORS
    )


def _collects(call: ast.Call, src: Source) -> bool:
    """A call that keeps a row as-is: another row, or adding it to a collection."""
    if any(_sdk(src, call.func, row) for row in _ROWS):
        return True
    return _adds_to_collection(call)


def _report_site(leaf: ast.Call, src: Source, parents: _Parents) -> ast.AST | None:
    """The ``raise`` or preflight row that reports ``leaf``; ``None`` if unmodelled."""
    parent, field, _ = parents[leaf]
    if isinstance(parent, ast.Raise) and field == "exc":
        return parent
    if isinstance(parent, ast.Attribute) and parent.attr == "to_failure_details":
        call, call_field, _ = parents[parent]
        if not (isinstance(call, ast.Call) and call_field == "func" and not call.args):
            return None
        parent, field, _ = parents[call]
    if isinstance(parent, ast.keyword) and parent.arg == "error":
        row = parents[parent][0]
        if isinstance(row, ast.Call) and any(_sdk(src, row.func, t) for t in _ROWS):
            return row
    return None


def _always_runs(
    site: ast.AST, src: Source, parents: _Parents, handler: ast.ExceptHandler
) -> bool:
    """Whether every path through ``handler`` reaches the report ``site``."""
    for parent, field, index in _ancestry(site, parents, handler):
        if field not in _ALWAYS_RUNS.get(type(parent), ()):
            return False
        if isinstance(parent, ast.Call) and not _collects(parent, src):
            return False
        if isinstance(parent, ast.Expr) and not _adds_to_collection(parent.value):
            return False
        if isinstance(parent, (ast.Try, ast.TryStar)) and field == "body":
            if parent.handlers:
                return False
        if field in _BLOCKS:
            if any(_can_exit(stmt) for stmt in _block(parent, field)[:index]):
                return False
    return True


def _fixed_leaf(
    checker: _Checker, src: Source, handler: ast.ExceptHandler
) -> ast.expr | None:
    """The auth or permission class ``handler`` reports on every path, if any.

    A leaf is a constructor call reported through ``_report_site``, or a class
    raised bare (``raise AuthError``), whose report site is the ``raise``.
    """
    parents = _parents(handler)
    for node in _own_nodes(handler):
        if isinstance(node, ast.Raise) and isinstance(
            node.exc, ast.Name | ast.Attribute
        ):
            leaf_class: ast.expr = node.exc
        elif isinstance(node, ast.Call):
            leaf_class = node.func
        else:
            continue
        if not _customer_rooted(checker, src, leaf_class):
            continue
        site = node if isinstance(node, ast.Raise) else _report_site(node, src, parents)
        if site is not None and _always_runs(site, src, parents, handler):
            return leaf_class
    return None


def scan(reg: Registry) -> list[Finding]:
    checker = _Checker(reg)
    findings: list[Finding] = []
    for src, method in reachable_preflight_sites(reg):
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
                        f"Broad except in preflight_check reports a fixed "
                        f"{ast.unparse(leaf)} for every failure it catches, so "
                        "an empty credential or an unreachable host reads as a "
                        "customer credential or grant problem. Classify the caught "
                        "exception first (application_sdk.errors."
                        "classify_http_exception for httpx, or an isinstance chain) "
                        "and fall back to a leaf that does not blame the customer "
                        "(InternalError when the cause is unknown), or narrow the "
                        "except clause."
                    ),
                    directives=src.directives,
                )
            )
    return findings
