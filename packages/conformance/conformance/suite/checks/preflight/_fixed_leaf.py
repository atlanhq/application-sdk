"""F021 PreflightFixedLeafInBroadExcept (CONNECT-1358).

Flags a broad ``except`` inside ``preflight_check`` (or a helper it reaches)
that builds an Auth- or Permission-rooted error without looking at what it
caught. Every failure in the ``try`` then reaches the customer as a credential
or grant problem: an empty credential, a DNS failure and a 500 all read as
"grant access", and the ticket chases source-side grants that were never
missing. The SDK's own subclasses outside ``application_sdk.errors``
(``SqlClientAuthFailedError``, ``CredentialError``, ...) count too; a test pins
``_SDK_CUSTOMER_LEAVES`` to the SDK source so a new one cannot be missed.

The leaf is exempt only when the caught exception selects it, through one of
these on the leaf's own path:

* an enclosing ``if`` / ``while`` / conditional expression / ``match`` whose
  test reads the exception, or a name stored from it earlier, directly or
  through other stored names (``leaf = classify(exc)``, then
  ``advisory = isinstance(leaf, ...)``), with the leaf in the branch the test
  selects. The ``else`` and a catch-all ``case _:`` are the fallback for
  everything the test did not match, so
  ``exc if isinstance(exc, AppError) else AuthError(...)`` still fires. A test
  every caught exception passes (``if exc:``, ``exc is not None``,
  ``isinstance(exc, Exception)``) selects nothing;
* an enclosing call that receives the caught exception itself (a classifier
  taking the leaf as its default).

Storing a result is not enough on its own: ``leaf = classify(exc)`` or
``detail = redact(exc)`` followed by an unconditional ``AuthError(...)`` still
fires, and so does the fallthrough after ``if transient := is_transient(exc):
raise transient``. A nested ``except`` is judged on its own and never makes
its outer handler fire. Known limit: a negated guard
(``if not isinstance(exc, X): AuthError(...)``) reads as a selected branch,
not a fallback.
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
_NOT_CLASSIFIERS = frozenset(
    {
        "str",
        "repr",
        "format",
        "type",
        "print",
        "getattr",
        "hasattr",
        "safe_traceback",
        "sanitize_cause_repr",
    }
)
_CONDITIONS = (ast.If, ast.IfExp, ast.While)
_STORES = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)
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


def _receives(call: ast.Call, names: set[str]) -> bool:
    if isinstance(call.func, ast.Name) and call.func.id in _NOT_CLASSIFIERS:
        return False
    values = [*call.args, *(kw.value for kw in call.keywords if kw.arg != "cause")]
    return any(isinstance(v, ast.Name) and v.id in names for v in values)


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


def _derived_names(handler: ast.ExceptHandler, caught: str, before: int) -> set[str]:
    """``caught`` plus every name stored, before line ``before``, from one of them."""
    stores = [
        node
        for node in _own_nodes(handler)
        if isinstance(node, _STORES) and node.value is not None and node.lineno < before
    ]
    names = {caught}
    grew = True
    while grew:
        grew = False
        for node in stores:
            if node.value is None or not _mentions(node.value, names):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name) and name.id not in names:
                        names.add(name.id)
                        grew = True
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


def _selects(test: ast.expr, names: set[str], caught: str) -> bool:
    return _mentions(test, names) and not _always_true(test, caught)


_Parents = dict[ast.AST, tuple[ast.AST, str]]


def _parents(handler: ast.ExceptHandler) -> _Parents:
    parents: _Parents = {}
    for parent in [handler, *_own_nodes(handler)]:
        for field, value in ast.iter_fields(parent):
            values = value if isinstance(value, list) else [value]
            children: list[ast.AST] = [c for c in values if isinstance(c, ast.AST)]
            for child in children:
                parents[child] = (parent, field)
    return parents


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
    names = _derived_names(handler, caught, leaf.lineno)
    child: ast.AST = leaf
    while child is not handler:
        parent, field = parents[child]
        if (
            isinstance(parent, _CONDITIONS)
            and field == "body"
            and _selects(parent.test, names, caught)
        ):
            return True
        if (
            isinstance(parent, ast.Match)
            and not _catch_all(child)
            and _mentions(parent.subject, names)
        ):
            return True
        if isinstance(parent, ast.Call) and _receives(parent, {caught}):
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
