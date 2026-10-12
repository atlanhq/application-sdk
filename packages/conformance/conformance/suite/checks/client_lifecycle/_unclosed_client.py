"""P056 UnclosedBaseClient / P057 CloseOverrideSkipsSuper detectors.

Both rules key off one question: which classes are ``BaseClient`` subclasses?
:func:`collect_class_bases` records every ``ClassDef`` in a file with its base
keys, where a base that resolves through the file's imports to the SDK's
``BaseClient`` becomes the :data:`SDK_BASE` seed and any other base is its bare
name.  :func:`resolve_subclasses` closes over those records to a fixed point, so
the registry is transitive and cross-file (name-keyed, like the P003/P013
registry in ``prescriptions``).

``P056`` then walks each function on its own.  A local name bound to a
constructor call on a registered subclass is *closed* if the function enters it
with ``async with <name>`` or calls ``await <name>.close()`` inside any
``finally``; it has *escaped* — and is skipped — if the bare name is returned,
yielded, passed as an argument, stored, or referenced from a nested scope.
Everything else fires.  Comparisons (``client is not None``) and attribute
access (``client.fetch()``) neither close nor move the client.

``P057`` flags a ``close`` method defined directly on a registered subclass
whose body contains no ``super().close()`` (or explicit
``<Base>.close(self)``) call.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks._ast_common._imports import collect_import_origins
from conformance.suite.schema.findings import Finding

#: Fully-qualified import origins that name the SDK's ``BaseClient``.
_SDK_BASE_CLIENT_ORIGINS: frozenset[str] = frozenset(
    {
        "application_sdk.clients.BaseClient",
        "application_sdk.clients.base.BaseClient",
    }
)

#: Base key standing for "the SDK's BaseClient" in a :class:`ClassRecord`.
SDK_BASE = "<application_sdk.clients.base.BaseClient>"

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

# Parents under which a bare ``Name`` load hands the client to someone else.
_ESCAPE_PARENTS = (
    ast.Return,
    ast.Yield,
    ast.YieldFrom,
    ast.Assign,
    ast.AnnAssign,
    ast.NamedExpr,
    ast.keyword,
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Dict,
    ast.Starred,
)


@dataclass(frozen=True)
class ClassRecord:
    """One ``ClassDef`` and its base keys (``SDK_BASE`` or a bare base name)."""

    name: str
    bases: tuple[str, ...]
    node: ast.ClassDef


def _dotted(expr: ast.expr) -> str | None:
    """Return ``a.b.c`` for a Name/Attribute chain, else ``None``."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        head = _dotted(expr.value)
        return f"{head}.{expr.attr}" if head is not None else None
    return None


def _qualify(expr: ast.expr, origins: Mapping[str, str]) -> str | None:
    """Resolve a Name/Attribute chain to its import-qualified dotted path."""
    dotted = _dotted(expr)
    if dotted is None:
        return None
    head, _, rest = dotted.partition(".")
    origin = origins.get(head)
    if origin is None:
        return dotted
    return f"{origin}.{rest}" if rest else origin


def _bare_name(expr: ast.expr, origins: Mapping[str, str]) -> str | None:
    """Return the class name *expr* refers to, seen through an import alias."""
    qualified = _qualify(expr, origins)
    if qualified is None:
        return None
    return qualified.rsplit(".", 1)[-1]


def collect_class_bases(
    tree: ast.AST, origins: Mapping[str, str] | None = None
) -> list[ClassRecord]:
    """Record every ``ClassDef`` in *tree* with its base keys."""
    if origins is None:
        origins = collect_import_origins(tree)
    records: list[ClassRecord] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases: list[str] = []
        for base in node.bases:
            if _qualify(base, origins) in _SDK_BASE_CLIENT_ORIGINS:
                bases.append(SDK_BASE)
                continue
            name = _bare_name(base, origins)
            if name is not None:
                bases.append(name)
        records.append(ClassRecord(node.name, tuple(bases), node))
    return records


def resolve_subclasses(records: Iterable[ClassRecord]) -> frozenset[str]:
    """Names of classes that transitively derive from the SDK's ``BaseClient``."""
    pending = list(records)
    derived: set[str] = set()
    changed = True
    while changed:
        changed = False
        for record in pending:
            if record.name in derived:
                continue
            if any(base == SDK_BASE or base in derived for base in record.bases):
                derived.add(record.name)
                changed = True
    return frozenset(derived)


# --------------------------------------------------------------------------- #
# P057 — close() override without super().close()
# --------------------------------------------------------------------------- #


def _calls_base_close(func: ast.AST, base_names: frozenset[str]) -> bool:
    """True if *func* calls ``super().close()`` or ``<Base>.close(self)``."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if not isinstance(callee, ast.Attribute) or callee.attr != "close":
            continue
        receiver = callee.value
        if (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Name)
            and receiver.func.id == "super"
        ):
            return True
        if isinstance(receiver, ast.Name) and receiver.id in base_names:
            return True
    return False


def check_p057(
    records: Iterable[ClassRecord],
    subclasses: frozenset[str],
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P057 for each ``close`` override on a subclass that skips the base."""
    findings: list[Finding] = []
    for record in records:
        if record.name not in subclasses:
            continue
        base_names = frozenset(record.bases) | {"BaseClient"}
        for stmt in record.node.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if stmt.name != "close" or _calls_base_close(stmt, base_names):
                continue
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P057",
                    node=stmt,
                    message=(
                        f"{record.name}.close() overrides BaseClient.close() without "
                        "calling super().close(), so the client's shared httpx "
                        "connection pool is never released, even by callers that "
                        "close it. Add 'await super().close()' after the subclass "
                        "cleanup (see docs/concepts/clients.md)."
                    ),
                    directives=directives,
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# P056 — client constructed in a function and never closed
# --------------------------------------------------------------------------- #


def _walk_scope(root: ast.AST) -> Iterator[tuple[ast.AST, ast.AST]]:
    """Yield ``(node, parent)`` under *root*, not descending into nested scopes.

    Nested function / lambda / class nodes are yielded themselves (so a caller
    can see them) but their bodies are not entered.
    """
    stack: list[ast.AST] = [root]
    while stack:
        parent = stack.pop()
        for child in ast.iter_child_nodes(parent):
            yield child, parent
            if not isinstance(child, _SCOPE_NODES):
                stack.append(child)


def _constructed_class(
    value: ast.expr | None, origins: Mapping[str, str]
) -> str | None:
    """Return the class name if *value* is a direct constructor call."""
    if not isinstance(value, ast.Call):
        return None
    return _bare_name(value.func, origins)


def _is_close_call_on(node: ast.AST, name: str) -> bool:
    """True if *node* is ``await <name>.close()``."""
    if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
        return False
    callee = node.value.func
    return (
        isinstance(callee, ast.Attribute)
        and callee.attr == "close"
        and isinstance(callee.value, ast.Name)
        and callee.value.id == name
    )


@dataclass
class _Lifecycle:
    """How a function treats one locally constructed client."""

    closed: bool = False
    closed_outside_finally: bool = False
    escaped: bool = False


def _finally_nodes(func: ast.AST) -> set[int]:
    """``id()`` of every node inside a ``finally`` block of *func*'s own scope."""
    inside: set[int] = set()
    for node, _ in _walk_scope(func):
        if isinstance(node, (ast.Try, ast.TryStar)):
            for stmt in node.finalbody:
                inside.add(id(stmt))
                inside.update(id(child) for child, _ in _walk_scope(stmt))
    return inside


def _nested_scope_names(func: ast.AST) -> set[str]:
    """Names loaded anywhere inside a scope nested in *func*."""
    names: set[str] = set()
    for node, _ in _walk_scope(func):
        if isinstance(node, _SCOPE_NODES):
            names.update(
                sub.id
                for sub in ast.walk(node)
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
            )
    return names


def _lifecycle(func: ast.AST, name: str, in_finally: set[int]) -> _Lifecycle:
    """Classify how *func* closes, or hands off, the client bound to *name*."""
    state = _Lifecycle()
    for node, parent in _walk_scope(func):
        if isinstance(node, ast.AsyncWith):
            if any(
                isinstance(item.context_expr, ast.Name) and item.context_expr.id == name
                for item in node.items
            ):
                state.closed = True
        elif _is_close_call_on(node, name):
            if id(node) in in_finally:
                state.closed = True
            else:
                state.closed_outside_finally = True
        elif (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Load)
        ):
            if isinstance(parent, _ESCAPE_PARENTS):
                state.escaped = True
            elif isinstance(parent, ast.Call) and node is not parent.func:
                state.escaped = True
    return state


def _client_bindings(
    func: ast.AST, subclasses: frozenset[str], origins: Mapping[str, str]
) -> list[tuple[str, str, ast.stmt]]:
    """``(local_name, class_name, stmt)`` for each ``name = Subclass(...)``."""
    bindings: list[tuple[str, str, ast.stmt]] = []
    for node, _ in _walk_scope(func):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        cls = _constructed_class(value, origins)
        if cls is None or cls not in subclasses:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings.append((target.id, cls, node))
    return bindings


def check_p056(
    tree: ast.AST,
    subclasses: frozenset[str],
    filename: str,
    directives: dict[int, _IgnoreDirective],
    origins: Mapping[str, str] | None = None,
) -> list[Finding]:
    """Emit P056 for each locally constructed subclass the function never closes."""
    if not subclasses:
        return []
    if origins is None:
        origins = collect_import_origins(tree)
    findings: list[Finding] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bindings = _client_bindings(func, subclasses, origins)
        if not bindings:
            continue
        in_finally = _finally_nodes(func)
        captured = _nested_scope_names(func)
        reported: set[str] = set()
        for name, cls, stmt in bindings:
            if name in reported or name in captured:
                continue
            state = _lifecycle(func, name, in_finally)
            if state.closed or state.escaped:
                continue
            reported.add(name)
            if state.closed_outside_finally:
                how = (
                    f"'{name}.close()' is only reached when no exception is raised "
                    "before it"
                )
            else:
                how = f"'{name}' is never closed"
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P056",
                    node=stmt,
                    message=(
                        f"{cls} (a BaseClient subclass) is constructed in "
                        f"{func.name}() and {how}, so its shared httpx connection "
                        f"pool stays open. Use 'async with {cls}(...) as {name}:', "
                        f"or call 'await {name}.close()' in a finally (see "
                        "docs/concepts/clients.md)."
                    ),
                    directives=directives,
                )
            )
    return findings
