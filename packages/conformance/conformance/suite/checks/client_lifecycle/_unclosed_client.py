"""P056 UnclosedBaseClient / P057 CloseOverrideSkipsSuper detectors.

Both rules key off one question: which classes are ``BaseClient`` subclasses?
Classes are identified by their **module-qualified** name (``app.client.Client``),
never by bare name, so two unrelated ``Client`` classes in different modules stay
distinct.  :class:`ModuleIndex` parses one file into its module name, its import
bindings (absolute, relative and dotted), and the classes it defines;
:class:`Registry` resolves a reference to its defining class across every
indexed module — following re-exports such as ``from .client import Client`` in
a package ``__init__`` — and closes the subclass relation to a fixed point,
seeded only from the SDK's ``BaseClient``.

``P056`` then walks each function on its own and tracks each constructor
binding (``name = Subclass(...)``) from its line to the next rebinding of that
name.  Within that range the client is *closed* if the function enters it with
``async with <name>`` or calls ``await <name>.close()`` inside a ``finally``, on
every path from the binding to the end of the function (:class:`_MustRun`; a
guard inside the ``finally`` itself is the accepted ownership-guard shape); it
has *escaped* — and is skipped — if it is returned, yielded, passed as an
argument, stored on an attribute / in a container, or referenced from a nested
scope.  A plain local alias (``alias = client``) neither closes nor moves it: the
alias is tracked the same way.  Everything else fires.  Comparisons
(``client is not None``) and attribute access (``client.fetch()``) neither close
nor move the client.

``P057`` flags a ``close`` method defined directly on a registered subclass
whose own body (not a nested helper) never *awaits* ``super().close()`` (or an
explicit ``<Base>.close(self)`` on a strict ancestor) on every path through it.
``BaseClient.close()`` is a coroutine, so a bare call only builds a coroutine
object and releases nothing.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

#: Fully-qualified names that are the SDK's ``BaseClient``.
SDK_BASE_CLIENTS: frozenset[str] = frozenset(
    {
        "application_sdk.clients.BaseClient",
        "application_sdk.clients.base.BaseClient",
    }
)

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)

#: Upper bound on re-export hops when canonicalising a qualified name.
_MAX_REEXPORT_HOPS = 8

# Parents under which a bare ``Name`` load hands the client to someone else.
# ``ast.Assign`` / ``ast.AnnAssign`` are handled separately: a plain local alias
# is tracked, any other assignment target (attribute, subscript, unpacking) is
# an escape.
_ESCAPE_PARENTS = (
    ast.Return,
    ast.Yield,
    ast.YieldFrom,
    ast.NamedExpr,
    ast.keyword,
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Dict,
    ast.Starred,
)


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


def _dotted(expr: ast.expr) -> str | None:
    """Return ``a.b.c`` for a Name/Attribute chain, else ``None``."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        head = _dotted(expr.value)
        return f"{head}.{expr.attr}" if head is not None else None
    return None


#: Expression fields whose sub-expressions may not be evaluated.
_CONDITIONAL_EXPR_FIELDS: dict[type[ast.AST], frozenset[str]] = {
    ast.IfExp: frozenset({"body", "orelse"}),
    ast.BoolOp: frozenset({"values"}),
}

_Links = dict[int, tuple[ast.AST, str]]


def _parent_links(root: ast.AST) -> _Links:
    """Map ``id(node)`` to ``(parent, field)`` within *root*'s own scope."""
    links: _Links = {}
    stack: list[ast.AST] = [root]
    while stack:
        parent = stack.pop()
        for name, value in ast.iter_fields(parent):
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.AST):
                    links[id(child)] = (parent, name)
                    if not isinstance(child, _SCOPE_NODES):
                        stack.append(child)
    return links


class _MustRun:
    """Does every path through a block reach one of a set of *sites*?

    A site is a statement or expression node (``async with client``, ``await
    client.close()``, ``await super().close()``).  The analysis is structural,
    over statement blocks: a block reaches a site if any of its statements
    does; an ``if`` only if both arms do; a ``match`` only if every case does
    and the last is a bare ``case _``; a loop never (it may run zero times); a
    ``try`` if its ``finally`` does, or if its body or ``else`` does *and* every
    ``except`` handler does or re-raises — a handler that swallows the
    exception is a path on which the rest of the body never ran.  Sites inside a conditional expression (``a if c else b``, ``and`` /
    ``or``) do not count.  Early ``return`` / ``raise`` are not modelled.

    With *lenient_finally*, any site anywhere inside a ``finally`` counts, so
    ``finally: if owns_client: await client.close()`` — a guard that decides
    ownership, not whether cleanup runs — is accepted.
    """

    def __init__(
        self, sites: set[int], links: _Links, *, lenient_finally: bool
    ) -> None:
        self.sites = sites
        self.links = links
        self.lenient_finally = lenient_finally

    def block(self, stmts: list[ast.stmt]) -> bool:
        return any(self.stmt(s) for s in stmts)

    def _finally(self, stmts: list[ast.stmt]) -> bool:
        if not self.lenient_finally:
            return self.block(stmts)
        return any(
            id(node) in self.sites
            for stmt in stmts
            for node in (stmt, *(child for child, _ in _walk_scope(stmt)))
        )

    def _handlers(self, node: ast.Try | ast.TryStar) -> bool:
        """True if every ``except`` handler reaches a site or re-raises."""
        return all(
            self.block(h.body) or (bool(h.body) and isinstance(h.body[-1], ast.Raise))
            for h in node.handlers
        )

    def stmt(self, stmt: ast.stmt) -> bool:
        if id(stmt) in self.sites:
            return True
        if isinstance(stmt, ast.If):
            return self._expr(stmt.test, stmt) or (
                self.block(stmt.body) and self.block(stmt.orelse)
            )
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            return False
        if isinstance(stmt, (ast.Try, ast.TryStar)):
            return self._finally(stmt.finalbody) or (
                (self.block(stmt.body) or self.block(stmt.orelse))
                and self._handlers(stmt)
            )
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            return self.block(stmt.body)
        if isinstance(stmt, ast.Match):
            last = stmt.cases[-1].pattern if stmt.cases else None
            exhaustive = isinstance(last, ast.MatchAs) and last.pattern is None
            return exhaustive and all(self.block(c.body) for c in stmt.cases)
        if isinstance(stmt, _SCOPE_NODES):
            return False
        return self._expr(stmt, stmt)

    def _expr(self, root: ast.AST, stmt: ast.stmt) -> bool:
        """True if a site under *root* is evaluated whenever *stmt* runs."""
        for node, _ in _walk_scope(root):
            if id(node) in self.sites and self._unconditional(node, stmt):
                return True
        return False

    def _unconditional(self, node: ast.AST, stmt: ast.stmt) -> bool:
        current = node
        while current is not stmt and id(current) in self.links:
            parent, name = self.links[id(current)]
            if name in _CONDITIONAL_EXPR_FIELDS.get(type(parent), frozenset()):
                return False
            current = parent
        return True

    def after(self, stmt: ast.stmt, root: ast.AST) -> bool:
        """True if every path from *stmt* to the end of *root* reaches a site.

        Checks the statements after *stmt* in its own block, then each enclosing
        ``finally``, then climbs to the enclosing statement and repeats.  Leaving
        a ``try`` body early through a handler skips the rest of the body, so
        there the rest (or the ``else``) only counts if every handler also
        reaches a site or re-raises.
        """
        current: ast.AST = stmt
        while current is not root and id(current) in self.links:
            parent, name = self.links[id(current)]
            if isinstance(current, ast.stmt):
                block = getattr(parent, name)
                index = next(i for i, s in enumerate(block) if s is current)
                reached = self.block(block[index + 1 :])
                if isinstance(parent, (ast.Try, ast.TryStar)) and name == "body":
                    reached = (reached or self.block(parent.orelse)) and self._handlers(
                        parent
                    )
                if reached:
                    return True
            if (
                isinstance(parent, (ast.Try, ast.TryStar))
                and name != "finalbody"
                and self._finally(parent.finalbody)
            ):
                return True
            current = parent
        return False


def module_name_for(rel: str) -> tuple[str, bool]:
    """``(dotted module name, is_package)`` for a repo-relative ``.py`` path."""
    parts = rel.replace("\\", "/").removesuffix(".py").split("/")
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(p for p in parts if p), is_package


# --------------------------------------------------------------------------- #
# Module index + cross-module registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, eq=False)
class ClassRecord:
    """One ``ClassDef``: its qualified name, base expressions and owning module."""

    qualname: str
    node: ast.ClassDef
    module: ModuleIndex


@dataclass(eq=False)
class ModuleIndex:
    """One parsed file: module identity, import bindings and defined classes."""

    module: str
    is_package: bool
    tree: ast.AST
    bindings: dict[str, str] = field(default_factory=dict)
    classes: list[ClassRecord] = field(default_factory=list)

    @classmethod
    def build(cls, tree: ast.AST, rel: str) -> ModuleIndex:
        module, is_package = module_name_for(rel)
        index = cls(module=module, is_package=is_package, tree=tree)
        index._collect_bindings()
        top_level = {id(stmt) for stmt in getattr(tree, "body", [])}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                qualname = f"{module}.{node.name}" if module else node.name
                index.classes.append(ClassRecord(qualname, node, index))
                if id(node) in top_level:
                    # A module-level class shadows an import of the same name.
                    index.bindings[node.name] = qualname
                else:
                    index.bindings.setdefault(node.name, qualname)
        return index

    def _package(self, level: int) -> str | None:
        """The package a ``level``-dot relative import is anchored at."""
        parts = self.module.split(".") if self.module else []
        if not self.is_package:
            parts = parts[:-1]
        drop = level - 1
        if drop > len(parts):
            return None
        return ".".join(parts[: len(parts) - drop])

    def _collect_bindings(self) -> None:
        # Module scope only: an import inside a function or class body binds a
        # name in that scope, not in the module, so it must not change what a
        # module-level class base resolves to.  Function-local imports are
        # overlaid where they apply (see ``_FunctionScope.local_bindings``).
        self.bindings.update(
            self.import_bindings(node for node, _ in _walk_scope(self.tree))
        )

    def import_bindings(self, nodes: Iterable[ast.AST]) -> dict[str, str]:
        """Names bound by the ``import`` statements among *nodes*."""
        bindings: dict[str, str] = {}
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        bindings[alias.asname] = alias.name
                    else:
                        # ``import a.b.c`` binds ``a`` to the top-level package.
                        top = alias.name.split(".")[0]
                        bindings[top] = top
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = self._package(node.level)
                    if base is None:
                        continue
                    source = ".".join(p for p in (base, node.module or "") if p)
                else:
                    source = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    bound = alias.asname or alias.name
                    bindings[bound] = f"{source}.{alias.name}" if source else alias.name
        return bindings

    def qualify(
        self, expr: ast.expr, local: Mapping[str, str] | None = None
    ) -> str | None:
        """Resolve a Name/Attribute chain via *local* bindings, then the module's."""
        dotted = _dotted(expr)
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        origin = (local or {}).get(head) or self.bindings.get(head)
        if origin is None:
            return None
        return f"{origin}.{rest}" if rest else origin


class Registry:
    """Cross-module class lookup and the transitive ``BaseClient`` subclass set."""

    def __init__(self, modules: list[ModuleIndex]) -> None:
        self._modules = {m.module: m for m in modules}
        self._classes = {c.qualname: c for m in modules for c in m.classes}
        self.subclasses = self._resolve_subclasses()

    def canonical(self, qualname: str | None) -> str | None:
        """Follow re-exports until *qualname* names a defined class (or the SDK base)."""
        for _ in range(_MAX_REEXPORT_HOPS):
            if qualname is None or qualname in SDK_BASE_CLIENTS:
                return qualname
            if qualname in self._classes:
                return qualname
            module, _, name = qualname.rpartition(".")
            owner = self._modules.get(module)
            target = owner.bindings.get(name) if owner is not None else None
            if target is None or target == qualname:
                return qualname
            qualname = target
        return qualname

    def resolve(
        self,
        module: ModuleIndex,
        expr: ast.expr,
        local: Mapping[str, str] | None = None,
    ) -> str | None:
        """Canonical qualified name *expr* refers to inside *module*."""
        return self.canonical(module.qualify(expr, local))

    def is_client(
        self,
        module: ModuleIndex,
        expr: ast.expr,
        local: Mapping[str, str] | None = None,
    ) -> bool:
        """True if *expr* in *module* names a ``BaseClient`` subclass."""
        return self.resolve(module, expr, local) in self.subclasses

    def ancestors(self, qualname: str) -> frozenset[str]:
        """Every class *qualname* strictly inherits from (canonical names)."""
        found: set[str] = set()
        pending = [qualname]
        while pending:
            record = self._classes.get(pending.pop())
            if record is None:
                continue
            for base in record.node.bases:
                target = self.resolve(record.module, base)
                if target is not None and target not in found:
                    found.add(target)
                    pending.append(target)
        found.discard(qualname)
        return frozenset(found)

    def _resolve_subclasses(self) -> frozenset[str]:
        derived: set[str] = set()
        changed = True
        while changed:
            changed = False
            for record in self._classes.values():
                if record.qualname in derived:
                    continue
                for base in record.node.bases:
                    target = self.resolve(record.module, base)
                    if target in SDK_BASE_CLIENTS or target in derived:
                        derived.add(record.qualname)
                        changed = True
                        break
        return frozenset(derived)


# --------------------------------------------------------------------------- #
# P057 — close() override without an awaited super().close()
# --------------------------------------------------------------------------- #


def _awaits_base_close(func: ast.AST, record: ClassRecord, registry: Registry) -> bool:
    """True if *func*'s own scope awaits ``super().close()`` / ``<Base>.close(self)``.

    The call must be the operand of an ``await`` — ``BaseClient.close()`` is a
    coroutine function, so an un-awaited call releases nothing.  It must run on
    every path through the override: under an ``if`` it needs a call in both
    arms; under a loop or an ``except`` handler alone it does not count (see
    :class:`_MustRun`).  Nested helper functions are not entered: defining a helper
    that would close the base is not the same as closing it.  An explicit
    ``<Base>.close(self)`` only counts when ``<Base>`` is a strict ancestor —
    ``Client.close(self)`` inside ``Client.close`` recurses and never reaches it.
    """
    ancestors = registry.ancestors(record.qualname)
    sites: set[int] = set()
    for node, _ in _walk_scope(func):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        callee = node.value.func
        if not isinstance(callee, ast.Attribute) or callee.attr != "close":
            continue
        receiver = callee.value
        if (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Name)
            and receiver.func.id == "super"
        ) or registry.resolve(record.module, receiver) in (
            SDK_BASE_CLIENTS | ancestors
        ):
            sites.add(id(node))
    if not sites:
        return False
    must = _MustRun(sites, _parent_links(func), lenient_finally=False)
    return must.block(func.body)


def check_p057(
    module: ModuleIndex,
    registry: Registry,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P057 for each ``close`` override on a subclass that skips the base."""
    findings: list[Finding] = []
    for record in module.classes:
        if record.qualname not in registry.subclasses:
            continue
        for stmt in record.node.body:
            if not isinstance(stmt, _FUNC_NODES) or stmt.name != "close":
                continue
            if _awaits_base_close(stmt, record, registry):
                continue
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P057",
                    node=stmt,
                    message=(
                        f"{record.node.name}.close() overrides BaseClient.close() "
                        "without awaiting super().close(), so the client's shared "
                        "httpx connection pool is never released, even by callers "
                        "that close it. Add 'await super().close()' after the "
                        "subclass cleanup (see docs/concepts/clients.md)."
                    ),
                    directives=directives,
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# P056 — client constructed in a function and never closed
# --------------------------------------------------------------------------- #


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
    closed_on_some_paths: bool = False
    escaped: bool = False
    #: ``async with`` / ``close()``-in-``finally`` sites, and other ``close()`` sites.
    protected: set[int] = field(default_factory=set)
    unprotected: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class _Span:
    """One name holding the client, from the line it is bound to its next rebind."""

    name: str
    start: int
    end: float

    def covers(self, node: ast.AST) -> bool:
        line = getattr(node, "lineno", 0)
        return self.start <= line < self.end


class _FunctionScope:
    """Per-function facts P056 needs, computed once."""

    def __init__(self, func: ast.AST, module: ModuleIndex) -> None:
        self.func = func
        self.nodes = list(_walk_scope(func))
        self.links = _parent_links(func)
        self.local_bindings = module.import_bindings(node for node, _ in self.nodes)
        self.in_finally: set[int] = set()
        self.stores: dict[str, list[int]] = {}
        self.captured: set[str] = set()
        for node, _ in self.nodes:
            if isinstance(node, (ast.Try, ast.TryStar)):
                for stmt in node.finalbody:
                    self.in_finally.add(id(stmt))
                    self.in_finally.update(id(child) for child, _ in _walk_scope(stmt))
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.stores.setdefault(node.id, []).append(node.lineno)
            elif isinstance(node, _SCOPE_NODES):
                self.captured.update(
                    sub.id
                    for sub in ast.walk(node)
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
                )

    def span(self, name: str, start: int) -> _Span:
        """The span *name* holds what it was bound to at line *start*."""
        later = [line for line in self.stores.get(name, []) if line > start]
        return _Span(name, start, min(later) if later else float("inf"))


def _local_alias(parent: ast.AST, node: ast.Name) -> str | None:
    """Target name if *parent* is ``alias = <node>`` (a plain local rebinding)."""
    if isinstance(parent, ast.Assign) and parent.value is node:
        targets = parent.targets
    elif isinstance(parent, ast.AnnAssign) and parent.value is node:
        targets = [parent.target]
    else:
        return None
    if len(targets) == 1 and isinstance(targets[0], ast.Name):
        return targets[0].id
    return None


def _lifecycle(scope: _FunctionScope, first: _Span, binding: ast.stmt) -> _Lifecycle:
    """Classify how the function closes, or hands off, one constructed client.

    Follows the client through plain local aliases; each name is only
    considered between the line it takes the client and its next rebinding.
    Cleanup only counts when every path from the binding to the end of the
    function reaches it (:class:`_MustRun`) — sites in both arms of an ``if``
    combine.  A condition *inside* a ``finally`` is allowed — that is the
    ownership-guard shape (``finally: if owns_client: await client.close()``).
    """
    state = _Lifecycle()
    pending = [first]
    seen: set[_Span] = set()
    while pending:
        span = pending.pop()
        if span in seen:
            continue
        seen.add(span)
        if span.name in scope.captured:
            state.escaped = True
            continue
        for node, parent in scope.nodes:
            if not span.covers(node):
                continue
            if isinstance(node, ast.AsyncWith):
                if any(
                    isinstance(item.context_expr, ast.Name)
                    and item.context_expr.id == span.name
                    for item in node.items
                ):
                    state.protected.add(id(node))
            elif _is_close_call_on(node, span.name):
                if id(node) in scope.in_finally:
                    state.protected.add(id(node))
                else:
                    state.unprotected.add(id(node))
            elif (
                isinstance(node, ast.Name)
                and node.id == span.name
                and isinstance(node.ctx, ast.Load)
            ):
                alias = _local_alias(parent, node)
                if alias is not None:
                    pending.append(scope.span(alias, node.lineno))
                elif isinstance(parent, (ast.Assign, ast.AnnAssign, *_ESCAPE_PARENTS)):
                    state.escaped = True
                elif isinstance(parent, ast.Call) and node is not parent.func:
                    state.escaped = True
    if state.escaped:
        return state

    def reached(sites: set[int]) -> bool:
        must = _MustRun(sites, scope.links, lenient_finally=True)
        return must.after(binding, scope.func)

    if reached(state.protected):
        state.closed = True
    elif reached(state.protected | state.unprotected):
        state.closed_outside_finally = True
    elif state.protected or state.unprotected:
        state.closed_on_some_paths = True
    return state


def _client_bindings(
    scope: _FunctionScope, module: ModuleIndex, registry: Registry
) -> list[tuple[str, str, ast.stmt]]:
    """``(local_name, class_name, stmt)`` for each ``name = Subclass(...)``."""
    bindings: list[tuple[str, str, ast.stmt]] = []
    for node, _ in scope.nodes:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if not isinstance(value, ast.Call) or not registry.is_client(
            module, value.func, scope.local_bindings
        ):
            continue
        cls = _dotted(value.func) or "client"
        for target in targets:
            if isinstance(target, ast.Name):
                bindings.append((target.id, cls.rsplit(".", 1)[-1], node))
    return bindings


def check_p056(
    module: ModuleIndex,
    registry: Registry,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P056 for each locally constructed subclass the function never closes."""
    if not registry.subclasses:
        return []
    findings: list[Finding] = []
    for func in ast.walk(module.tree):
        if not isinstance(func, _FUNC_NODES):
            continue
        scope = _FunctionScope(func, module)
        for name, cls, stmt in _client_bindings(scope, module, registry):
            state = _lifecycle(scope, scope.span(name, stmt.lineno), stmt)
            if state.closed or state.escaped:
                continue
            if state.closed_on_some_paths:
                how = f"'{name}' is only closed on some paths through the function"
            elif state.closed_outside_finally:
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


__all__ = [
    "SDK_BASE_CLIENTS",
    "ClassRecord",
    "ModuleIndex",
    "Registry",
    "check_p056",
    "check_p057",
    "module_name_for",
]
