"""P052 EntitySerializationBypass — app serializes an asset around ``entity_bytes``.

``application_sdk.common.asset_serialization.entity_bytes`` is the SDK's one
serialization seam for a mapper result (FND-2056 / FND-2137): it owns the
dispatch, the ``connectionName`` injection, the declared entity envelope and the
placeholder-guid strip (FND-2720).  A fix made there reaches every connector
that goes through it — and none that does not.  An app that turns a pyatlan
asset into wire output itself (``asset.to_nested_bytes()``) silently opts out of
every one of those, and because reference apps are copied, the bypass spreads.

Per-file.  Flags, in hand-written app code only (``app/`` minus
``app/generated/``; ``tests/`` never reaches the scan — see ``discover``):

* any ``<x>.to_nested_bytes(...)`` / ``<x>.to_nested_dict(...)`` call.  The
  receiver is not resolved: both names are pyatlan-asset serializers and no
  other type in an app carries them.
* a call that resolves to an encoder which skips the seam: ``pyatlan_v9``'s
  ``to_atlas_format``, or the SDK's own
  ``application_sdk.common.entity_envelope.to_atlas_format_dict`` (the helper
  ``entity_bytes`` calls internally — public, so an app can reach it).

Name resolution
---------------
Names resolve by lexical scope, the way Python binds them: the call's own
scope, then the enclosing function scopes, then the module.  Class bodies are
skipped, as Python skips them, and comprehensions get a scope of their own.

A name can have several possible bindings at a call, and the rule fires when
*any* of them is a bypass.  It is a warning, so an uncertain binding stays
visible instead of silently passing:

* **In the call's own scope** control flow decides.  The latest earlier binding
  whose block encloses the call definitely runs and hides everything before it.
  A later binding inside a branch, loop or ``try`` the call is not in may or may
  not run, so it is a candidate alongside the definite one.
* **In an enclosing scope** line order means nothing: a function body reads a
  module global when it is *called*, possibly after a later rebinding.  Every
  binding of the name there is a candidate.

A simple local alias is followed — ``encode = asset.to_nested_bytes``,
``enc = to_atlas_format``, chained ``a = b = …`` — so saving the callable before
calling it does not hide the bypass.  Deeper indirection (``getattr``,
``functools.partial``, containers, attribute stores) is out of scope.

A call to ``entity_bytes`` itself is never flagged, and neither is anything it
calls internally — the SDK is not in scope.

WARN tier.  A genuine non-entity use — a ``ConnectionRef`` built from
``to_atlas_format``, as the SDK's own ``contracts/types.py`` does — is the one
sanctioned carve-out, suppressed inline with a reason.
"""

from __future__ import annotations

import ast
import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import PurePath

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

#: Asset methods that emit the wire shape directly.
_ASSET_SERIALIZERS: frozenset[str] = frozenset({"to_nested_bytes", "to_nested_dict"})

#: The pyatlan_v9 module-level encoder, matched only when it resolves there.
_ATLAS_FORMAT = "to_atlas_format"
_PYATLAN_V9 = "pyatlan_v9"

#: The SDK's internal flattening helper, and the modules it is importable from:
#: ``entity_envelope`` defines and exports it; ``asset_serialization`` imports
#: it at module level, so ``from …asset_serialization import
#: to_atlas_format_dict`` resolves too, ``__all__`` notwithstanding.
_SDK_ATLAS_FORMAT_DICT = "to_atlas_format_dict"
_SDK_ATLAS_FORMAT_DICT_MODULES: frozenset[str] = frozenset(
    {
        "application_sdk.common.entity_envelope",
        "application_sdk.common.asset_serialization",
    }
)

#: Bound on alias-chain hops, so a cyclic ``a = b; b = a`` cannot loop.
_MAX_ALIAS_DEPTH = 8

#: Fields of a compound statement (or handler / match case) holding a block
#: that may or may not run relative to its siblings.  ``finalbody`` is absent
#: on purpose: a ``finally`` block always runs before the statement after the
#: ``try`` is reached, so its bindings are definite there.
_BLOCK_FIELDS: frozenset[str] = frozenset({"body", "orelse", "handlers", "cases"})

_HINT = (
    "Serialize through "
    "`application_sdk.common.asset_serialization.entity_bytes(asset, envelope=...)`"
)


def _in_app_source(filename: str) -> bool:
    """True for hand-written app code: under ``app/`` but not ``app/generated/``."""
    parts = PurePath(filename).parts
    if not parts or parts[0] != "app":
        return False
    return not (len(parts) > 2 and parts[1] == "generated")


def _dotted_parts(node: ast.expr) -> list[str] | None:
    """Flatten ``a.b.c`` to ``["a", "b", "c"]``; ``None`` if not all names."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return list(reversed(parts))


def _classify_origin(parts: list[str]) -> str | None:
    """Describe the encoder a fully-qualified path names, or ``None``."""
    if parts[0] == _PYATLAN_V9 and parts[-1] == _ATLAS_FORMAT:
        return "`pyatlan_v9` `to_atlas_format()`"
    if (
        parts[-1] == _SDK_ATLAS_FORMAT_DICT
        and ".".join(parts[:-1]) in _SDK_ATLAS_FORMAT_DICT_MODULES
    ):
        return "`to_atlas_format_dict()`"
    return None


# ── Scope model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Import:
    """A name bound by an import to the dotted path ``origin``."""

    origin: tuple[str, ...]


@dataclass(frozen=True)
class _Alias:
    """``name = <value>`` — a simple-name assignment that may alias a callable."""

    value: ast.expr


@dataclass(frozen=True)
class _Other:
    """Any other binding (parameter, def, loop target, …): shadows, names nothing."""


_Binding = _Import | _Alias | _Other

#: Where a statement sits inside its scope: the ids of the blocks enclosing it,
#: outermost first.  A binding's block encloses a use when its path is a prefix
#: of the use's path.
_Path = tuple[int, ...]


@dataclass(frozen=True)
class _Site:
    """One binding of a name: where it happens and what it binds."""

    lineno: int
    path: _Path
    binding: _Binding


@dataclass(frozen=True)
class _Use:
    """Where a name is read: its scope, line and block path."""

    scope: _Scope
    lineno: int
    path: _Path


@dataclass(eq=False)
class _Scope:
    parent: _Scope | None
    #: For a scope that runs inline where it is written (a comprehension), the
    #: point in the parent it runs at, so the parent is read with control flow.
    #: ``None`` for a function or class body, which reads the parent at runtime.
    entry: _Use | None = None
    bindings: dict[str, list[_Site]] = field(default_factory=dict)

    def bind(self, name: str, site: _Site) -> None:
        self.bindings.setdefault(name, []).append(site)

    def candidates(self, name: str, use: _Use) -> Iterator[tuple[_Scope, _Site]]:
        """Every binding *name* may hold at *use* (see the module docstring)."""
        scope: _Scope | None = self
        at: _Use | None = use
        while scope is not None:
            sites = scope.bindings.get(name)
            if sites:
                chosen = sites if at is None else _reaching(sites, at)
                yield from ((scope, site) for site in chosen)
                return
            at, scope = scope.entry, scope.parent


def _reaching(sites: list[_Site], use: _Use) -> list[_Site]:
    """The bindings in the use's own scope that may be live at *use*."""
    earlier = [s for s in sites if s.lineno <= use.lineno]
    if not earlier:
        # Bound in this scope only below the use — a loop body reaching round,
        # or code that fails with UnboundLocalError. Keep them all visible.
        return sites
    definite = [s for s in earlier if use.path[: len(s.path)] == s.path]
    if not definite:
        return earlier
    last = max(definite, key=lambda s: s.lineno)
    return [last, *(s for s in earlier if s.lineno > last.lineno)]


class _ScopeBuilder(ast.NodeVisitor):
    """Record every name binding per lexical scope, and the scope of each call."""

    def __init__(self) -> None:
        self._ids = itertools.count()
        self._scope = _Scope(parent=None)
        # The scope a function defined here closes over. A class body is not
        # visible to its methods, so entering one does not move this.
        self._closure = self._scope
        self._path: _Path = (next(self._ids),)
        self.calls: list[tuple[ast.Call, _Use]] = []

    def _bind(self, name: str, lineno: int, binding: _Binding) -> None:
        self._scope.bind(name, _Site(lineno, self._path, binding))

    def _enter(self, scope: _Scope, closure: _Scope) -> tuple[_Scope, _Scope, _Path]:
        saved = (self._scope, self._closure, self._path)
        self._scope, self._closure = scope, closure
        self._path = (next(self._ids),)
        return saved

    def _leave(self, saved: tuple[_Scope, _Scope, _Path]) -> None:
        self._scope, self._closure, self._path = saved

    # Scopes

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    ) -> None:
        # Decorators, defaults and annotations evaluate in the defining scope.
        if not isinstance(node, ast.Lambda):
            for deco in node.decorator_list:
                self.visit(deco)
            if node.returns is not None:
                self.visit(node.returns)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        if not isinstance(node, ast.Lambda):
            self._bind(node.name, node.lineno, _Other())
        saved = self._enter(_Scope(parent=self._closure), self._closure)
        self._closure = self._scope
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            self._bind(arg.arg, node.lineno, _Other())
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                self._bind(arg.arg, node.lineno, _Other())
        for stmt in node.body if isinstance(node.body, list) else [node.body]:
            self.visit(stmt)
        self._leave(saved)

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function
    visit_Lambda = _visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expr in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(expr)
        self._bind(node.name, node.lineno, _Other())
        saved = self._enter(_Scope(parent=self._scope), self._closure)
        for stmt in node.body:
            self.visit(stmt)
        self._leave(saved)

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
    ) -> None:
        # The first iterable evaluates in the enclosing scope; everything else,
        # including every target, belongs to the comprehension's own scope.
        first, *rest = node.generators
        self.visit(first.iter)
        # Inside a class body the comprehension skips the class scope, so the
        # scope it reads is not the one it is written in: no flow to borrow.
        entry = (
            _Use(self._scope, node.lineno, self._path)
            if self._scope is self._closure
            else None
        )
        saved = self._enter(_Scope(parent=self._closure, entry=entry), self._closure)
        self._closure = self._scope
        self.visit(first.target)
        for cond in first.ifs:
            self.visit(cond)
        for gen in rest:
            self.visit(gen)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self._leave(saved)

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension

    # Blocks — each statement list of a compound statement may or may not run.

    def _visit_compound(self, node: ast.AST) -> None:
        for name, value in ast.iter_fields(node):
            if name in _BLOCK_FIELDS and isinstance(value, list):
                # One id per block; each handler / match case opens its own
                # (see _visit_alternative), as they are alternatives.
                outer = self._path
                self._path = (*outer, next(self._ids))
                for item in value:
                    if isinstance(item, (ast.ExceptHandler, ast.match_case)):
                        self._path = outer
                    self.visit(item)
                self._path = outer
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item)
            elif name == "target" and isinstance(node, (ast.For, ast.AsyncFor)):
                # A loop that runs zero times never binds its target.
                outer = self._path
                self._path = (*outer, next(self._ids))
                self.visit(value)
                self._path = outer
            elif isinstance(value, ast.AST):
                self.visit(value)

    visit_If = _visit_compound
    visit_For = _visit_compound
    visit_AsyncFor = _visit_compound
    visit_While = _visit_compound
    visit_With = _visit_compound
    visit_AsyncWith = _visit_compound
    visit_Try = _visit_compound
    visit_Match = _visit_compound

    def visit_TryStar(self, node: ast.AST) -> None:  # Python >= 3.11
        self._visit_compound(node)

    def _visit_alternative(self, node: ast.ExceptHandler | ast.match_case) -> None:
        outer = self._path
        self._path = (*outer, next(self._ids))
        if isinstance(node, ast.ExceptHandler) and node.name:
            self._bind(node.name, node.lineno, _Other())
        self._visit_compound_fields(node)
        self._path = outer

    def _visit_compound_fields(self, node: ast.AST) -> None:
        # Children of a handler / case all run together once it is chosen.
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item)
            elif isinstance(value, ast.AST):
                self.visit(value)

    visit_ExceptHandler = _visit_alternative
    visit_match_case = _visit_alternative

    # Bindings

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            dotted = tuple(alias.name.split("."))
            if alias.asname:
                self._bind(alias.asname, node.lineno, _Import(dotted))
            else:
                # ``import a.b.c`` binds ``a`` to the top-level package.
                self._bind(dotted[0], node.lineno, _Import(dotted[:1]))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = tuple((node.module or "").split(".")) if node.level == 0 else None
        for alias in node.names:
            bound = alias.asname or alias.name
            if module is None or alias.name == "*":
                self._bind(bound, node.lineno, _Other())
            else:
                self._bind(bound, node.lineno, _Import((*module, alias.name)))

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        # ``a = b = value`` binds every simple-name target to the same value.
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._bind(target.id, node.lineno, _Alias(node.value))
            else:
                self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._bind(node.target.id, node.lineno, _Alias(node.value))
        else:
            self.visit(node.target)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(node.id, node.lineno, _Other())

    # Uses

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((node, _Use(self._scope, node.lineno, self._path)))
        self.generic_visit(node)


def _resolve(expr: ast.expr, use: _Use, depth: int = 0) -> str | None:
    """Describe a seam-bypassing serializer *expr* may name at *use*, or ``None``."""
    if depth > _MAX_ALIAS_DEPTH:
        return None
    if isinstance(expr, ast.Attribute) and expr.attr in _ASSET_SERIALIZERS:
        return f"`.{expr.attr}()`"
    parts = _dotted_parts(expr)
    if parts is None:
        return None
    rest = parts[1:]
    for scope, site in use.scope.candidates(parts[0], use):
        binding = site.binding
        what: str | None = None
        if isinstance(binding, _Import):
            what = _classify_origin([*binding.origin, *rest])
        elif isinstance(binding, _Alias):
            # ``t = transform; t.to_atlas_format(x)``: re-root the rest of the
            # attribute chain on the aliased value and resolve that where it
            # was bound.
            value = binding.value
            for attr in rest:
                value = ast.Attribute(value=value, attr=attr, ctx=ast.Load())
            what = _resolve(value, _Use(scope, site.lineno, site.path), depth + 1)
        if what is not None:
            return what
    return None


def check_p052(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P052 for asset serialization that does not go through ``entity_bytes``."""
    if not _in_app_source(filename):
        return []
    builder = _ScopeBuilder()
    builder.visit(tree)
    findings: list[Finding] = []
    for call, use in builder.calls:
        what = _resolve(call.func, use)
        if what is None:
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P052",
                node=call,
                message=(
                    f"asset serialized with {what}, bypassing the SDK's "
                    "serialization seam — connectionName injection, the entity "
                    "envelope and placeholder-guid stripping never apply. "
                    f"{_HINT}."
                ),
                directives=directives,
            )
        )
    return findings
