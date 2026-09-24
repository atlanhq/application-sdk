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

Resolution is by lexical scope, the way Python binds names: a call's name is
looked up in its own function scope, then the enclosing function scopes, then
the module (class bodies are skipped, as Python skips them).  Within a scope the
latest binding at or before the call wins.  So a parameter or local helper that
shadows an imported encoder is not flagged, and an import in one function does
not leak into another.  A simple local alias is followed — ``encode =
asset.to_nested_bytes`` or ``enc = to_atlas_format`` — so saving the callable
before calling it does not hide the bypass.  Deeper indirection (``getattr``,
``functools.partial``, containers) is out of scope.

A call to ``entity_bytes`` itself is never flagged, and neither is anything it
calls internally — the SDK is not in scope.

WARN tier.  A genuine non-entity use — a ``ConnectionRef`` built from
``to_atlas_format``, as the SDK's own ``contracts/types.py`` does — is the one
sanctioned carve-out, suppressed inline with a reason.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePath

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

#: Asset methods that emit the wire shape directly.
_ASSET_SERIALIZERS: frozenset[str] = frozenset({"to_nested_bytes", "to_nested_dict"})

#: The pyatlan_v9 module-level encoder, matched only when it resolves there.
_ATLAS_FORMAT = "to_atlas_format"
_PYATLAN_V9 = "pyatlan_v9"

#: The SDK's internal flattening helper, and the one module that exports it.
_SDK_ATLAS_FORMAT_DICT = "to_atlas_format_dict"
_SDK_ENVELOPE_MODULE = "application_sdk.common.entity_envelope"

#: Bound on alias-chain hops, so a cyclic ``a = b; b = a`` cannot loop.
_MAX_ALIAS_DEPTH = 8

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
        and ".".join(parts[:-1]) == _SDK_ENVELOPE_MODULE
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
    """``name = <value>`` — a single-name assignment that may alias a callable."""

    value: ast.expr


@dataclass(frozen=True)
class _Other:
    """Any other binding (parameter, def, loop target, …): shadows, names nothing."""


_Binding = _Import | _Alias | _Other


@dataclass
class _Scope:
    parent: _Scope | None
    bindings: dict[str, list[tuple[int, _Binding]]] = field(default_factory=dict)

    def bind(self, name: str, lineno: int, binding: _Binding) -> None:
        self.bindings.setdefault(name, []).append((lineno, binding))

    def lookup(self, name: str, lineno: int) -> tuple[_Scope, int, _Binding] | None:
        """The binding *name* resolves to from a use at *lineno*, walking outward.

        The latest binding at or before *lineno* wins; if the scope binds the
        name only later (a function body referring to a module name assigned
        below it), the latest binding in that scope stands in.
        """
        scope: _Scope | None = self
        while scope is not None:
            entries = scope.bindings.get(name)
            if entries:
                preceding = [e for e in entries if e[0] <= lineno]
                line, binding = max(preceding or entries, key=lambda e: e[0])
                return scope, line, binding
            scope = scope.parent
        return None


class _ScopeBuilder(ast.NodeVisitor):
    """Record every name binding per lexical scope, and the scope of each call."""

    def __init__(self) -> None:
        self._scope = _Scope(parent=None)
        # The scope a function defined here closes over. A class body is not
        # visible to its methods, so entering one does not move this.
        self._closure = self._scope
        self.calls: list[tuple[ast.Call, _Scope]] = []

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
            self._scope.bind(node.name, node.lineno, _Other())
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        inner = _Scope(parent=self._closure)
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            inner.bind(arg.arg, node.lineno, _Other())
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                inner.bind(arg.arg, node.lineno, _Other())
        outer, outer_closure = self._scope, self._closure
        self._scope = self._closure = inner
        for stmt in node.body if isinstance(node.body, list) else [node.body]:
            self.visit(stmt)
        self._scope, self._closure = outer, outer_closure

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function
    visit_Lambda = _visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expr in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(expr)
        self._scope.bind(node.name, node.lineno, _Other())
        outer = self._scope
        self._scope = _Scope(parent=outer)
        for stmt in node.body:
            self.visit(stmt)
        self._scope = outer

    # Bindings

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            dotted = tuple(alias.name.split("."))
            if alias.asname:
                self._scope.bind(alias.asname, node.lineno, _Import(dotted))
            else:
                # ``import a.b.c`` binds ``a`` to the top-level package.
                self._scope.bind(dotted[0], node.lineno, _Import(dotted[:1]))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = tuple((node.module or "").split(".")) if node.level == 0 else None
        for alias in node.names:
            bound = alias.asname or alias.name
            if module is None or alias.name == "*":
                self._scope.bind(bound, node.lineno, _Other())
            else:
                self._scope.bind(bound, node.lineno, _Import((*module, alias.name)))

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._scope.bind(node.targets[0].id, node.lineno, _Alias(node.value))
            return
        for target in node.targets:
            self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._scope.bind(node.target.id, node.lineno, _Alias(node.value))
        else:
            self.visit(node.target)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._scope.bind(node.id, node.lineno, _Other())

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._scope.bind(node.name, node.lineno, _Other())
        self.generic_visit(node)

    # Uses

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((node, self._scope))
        self.generic_visit(node)


def _resolve(expr: ast.expr, scope: _Scope, lineno: int, depth: int = 0) -> str | None:
    """Describe the seam-bypassing serializer *expr* names, or ``None``."""
    if depth > _MAX_ALIAS_DEPTH:
        return None
    if isinstance(expr, ast.Attribute) and expr.attr in _ASSET_SERIALIZERS:
        return f"`.{expr.attr}()`"
    parts = _dotted_parts(expr)
    if parts is None:
        return None
    found = scope.lookup(parts[0], lineno)
    if found is None:
        return None
    bound_scope, bound_line, binding = found
    rest = parts[1:]
    if isinstance(binding, _Import):
        return _classify_origin([*binding.origin, *rest])
    if isinstance(binding, _Alias):
        # ``t = transform; t.to_atlas_format(x)``: re-root the rest of the
        # attribute chain on the aliased value and resolve that where it was
        # bound.
        value = binding.value
        for attr in rest:
            value = ast.Attribute(value=value, attr=attr, ctx=ast.Load())
        return _resolve(value, bound_scope, bound_line, depth + 1)
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
    for call, scope in builder.calls:
        what = _resolve(call.func, scope, call.lineno)
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
