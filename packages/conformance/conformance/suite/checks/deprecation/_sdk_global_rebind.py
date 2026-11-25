"""B010 ``SdkGlobalRebind`` — flag an app monkeypatching the SDK at runtime.

Runs against *consumer apps* (scope ``app``). Flags any statement that rebinds
or deletes an attribute of something the file imported from
``application_sdk`` — a module, a class, any object:

* ``svc._CREDENTIAL_KEYS = svc._CREDENTIAL_KEYS | {"kerberos"}`` after
  ``import application_sdk.handler.service as svc``;
* ``service._normalize_credentials = wrapper`` after
  ``from application_sdk.handler import service``;
* ``Handler.some_hook = patched`` after
  ``from application_sdk.handler.base import Handler``;
* ``application_sdk.constants.X = 1`` after ``import application_sdk.constants``;
* ``x.attr += 1``, ``x.attr: T = v``, ``del x.attr``, ``for x.attr in ...``,
  ``with ... as x.attr``, and ``setattr(x, "attr", v)`` / ``delattr(x, "attr")``
  on any such ``x``.

Why this rule exists
--------------------

FND-4688. Two connector apps changed the SDK's request normalization by
rebinding globals of ``application_sdk.handler.service`` at import time: one
extended ``_CREDENTIAL_KEYS`` with ``kerberos``, the other replaced
``_normalize_credentials`` with a wrapper. The SDK kept extra indirection
layers only so those patches kept working, and any refactor of the handler
would have broken both apps silently — a rebound name nothing reads anymore
raises nothing. The patches also never reached the consolidated API host,
which does not import that module, so the behaviour already differed between
surfaces.

B008 reports the private *name* only in some of these shapes (not
``from application_sdk.handler import service``, which binds a module through
a public path), and not at all when the rebound name is public. The hazard is
the rebind, whatever the name looks like: the SDK reads its own globals and
class attributes on the assumption that it set them.

What is not flagged
-------------------

* An attribute of an object the app built (``client = SQLClient(); client.x =
  1``): it is the app's own state, and its root name is not an import.
* A name that a scope binds to something else: a parameter, a local
  assignment, a loop or ``with`` target, a nested ``def``/``class``, a
  comprehension variable. ``def update(service): service.option = 1`` mutates
  the argument, not the SDK module imported as ``service`` at the top of the
  file. The same holds at module level: a name the module both imports from
  the SDK and assigns otherwise (``except ImportError: service = None``) is
  ambiguous, and is not flagged.
* ``monkeypatch.setattr(...)`` and ``unittest.mock.patch(...)`` in tests: both
  undo the change when the test ends, and neither is the ``setattr`` builtin.
* A relative import, or an import rooted at the app's own packages.

Tier
----

WARN, not BLOCK: the fix is app-side. The key-set patch needs no replacement
(the SDK now reads an app's extra credential keys from its generated credential
form); the wrapper patch is a request shape to fix at its source.
"""

from __future__ import annotations

import ast
from collections.abc import Callable

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_Emit = Callable[[ast.AST, str], None]

_RULE_ID = "B010"
_SDK_ROOT = "application_sdk"

#: Rebinds with a known public replacement, keyed by the dotted path rebound.
_REPLACEMENTS: dict[str, str] = {
    "application_sdk.handler.service._CREDENTIAL_KEYS": (
        "delete the rebind: the SDK lifts every top-level field of the app's "
        "generated credential form (contract/app.pkl) into the credentials, on "
        "the worker and the API host alike"
    ),
    "application_sdk.handler.service._normalize_credentials": (
        "send the fields the entry point's input contract declares; a legacy "
        "request shape the contract cannot express is a gap to raise with the "
        "SDK, not a request body to rewrite in the app"
    ),
}


def _is_sdk(dotted: str) -> bool:
    return dotted == _SDK_ROOT or dotted.startswith(_SDK_ROOT + ".")


_FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPREHENSION = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


class _ScopeNames:
    """What one scope binds: SDK imports, every other binding, and declarations."""

    def __init__(self) -> None:
        self.sdk: dict[str, str] = {}
        self.other: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()


def _argument_names(args: ast.arguments) -> set[str]:
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg is not None:
        every.append(args.vararg)
    if args.kwarg is not None:
        every.append(args.kwarg)
    return {a.arg for a in every}


def _nested_scope_outer_parts(
    node: ast.AST, names: _ScopeNames
) -> list[ast.AST] | None:
    """For a node that opens a nested scope, the parts evaluated in this one.

    A nested ``def``/``class`` binds its own name here and evaluates its
    decorators (and a ``def`` its defaults, a ``class`` its bases) here; its
    body is its own scope. A comprehension evaluates only its first iterable
    here. ``None`` for a node that opens no scope.
    """
    if isinstance(node, _FUNCTION):
        names.other.add(node.name)
        return [
            *node.decorator_list,
            *node.args.defaults,
            *(d for d in node.args.kw_defaults if d is not None),
        ]
    if isinstance(node, ast.ClassDef):
        names.other.add(node.name)
        return [*node.decorator_list, *node.bases, *node.keywords]
    if isinstance(node, ast.Lambda):
        return [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]
    if isinstance(node, _COMPREHENSION):
        return [node.generators[0].iter]
    return None


def _record_import(node: ast.Import | ast.ImportFrom, names: _ScopeNames) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            if not _is_sdk(alias.name):
                names.other.add(local)
            else:
                # `import application_sdk.a.b` binds `application_sdk`.
                names.sdk[local] = alias.name if alias.asname else _SDK_ROOT
        return
    sdk = not node.level and _is_sdk(node.module or "")
    for alias in node.names:
        if alias.name == "*":
            continue
        local = alias.asname or alias.name
        if sdk:
            names.sdk[local] = f"{node.module}.{alias.name}"
        else:
            names.other.add(local)


def _bound_name(node: ast.AST) -> str | None:
    """The plain name *node* binds in its scope, if it binds one."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
        return node.id
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    return None


def _scope_names(body: list[ast.AST], args: ast.arguments | None) -> _ScopeNames:
    """The names one scope binds, without descending into nested scopes."""
    names = _ScopeNames()
    if args is not None:
        names.other |= _argument_names(args)
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        outer = _nested_scope_outer_parts(node, names)
        if outer is not None:
            stack.extend(outer)
            continue
        if isinstance(node, ast.Global):
            names.global_names.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            names.nonlocal_names.update(node.names)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            _record_import(node, names)
        else:
            bound = _bound_name(node)
            if bound:
                names.other.add(bound)
        stack.extend(ast.iter_child_nodes(node))
    return names


def _bindings(
    names: _ScopeNames,
    enclosing: dict[str, str],
    module: dict[str, str],
) -> dict[str, str]:
    """The SDK aliases in force in a scope that binds *names*.

    A name the scope binds to anything but an SDK import shadows the enclosing
    alias. A name it binds both ways is ambiguous, and dropped.
    """
    resolved = {
        name: path
        for name, path in enclosing.items()
        if name not in names.other and name not in names.sdk
    }
    for name, path in names.sdk.items():
        if name not in names.other:
            resolved[name] = path
    for name in names.global_names:
        resolved.pop(name, None)
        if name in module:
            resolved[name] = module[name]
    for name in names.nonlocal_names:
        resolved.pop(name, None)
        if name in enclosing:
            resolved[name] = enclosing[name]
    return resolved


def _dotted(node: ast.expr, bound: dict[str, str]) -> str | None:
    """The SDK path *node* names, when it is an attribute chain on an SDK import."""
    parts: list[str] = []
    cursor = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if not isinstance(cursor, ast.Name):
        return None
    base = bound.get(cursor.id)
    if base is None:
        return None
    return ".".join([base, *reversed(parts)])


def _targets(node: ast.AST) -> list[ast.expr]:
    """The expressions *node* binds or deletes, tuple and list targets unpacked."""
    raw: list[ast.expr]
    if isinstance(node, ast.Assign | ast.Delete):
        raw = list(node.targets)
    elif isinstance(
        node, ast.AugAssign | ast.AnnAssign | ast.For | ast.AsyncFor | ast.comprehension
    ):
        raw = [node.target]
    elif isinstance(node, ast.With | ast.AsyncWith):
        raw = [item.optional_vars for item in node.items if item.optional_vars]
    else:
        return []
    out: list[ast.expr] = []
    while raw:
        target = raw.pop()
        if isinstance(target, ast.Tuple | ast.List):
            raw.extend(target.elts)
        elif isinstance(target, ast.Starred):
            raw.append(target.value)
        else:
            out.append(target)
    return out


def _setattr_path(node: ast.Call, bound: dict[str, str]) -> str | None:
    """``setattr(x, "name", v)`` / ``delattr(x, "name")`` on an SDK import."""
    if not isinstance(node.func, ast.Name) or node.func.id not in {
        "setattr",
        "delattr",
    }:
        return None
    if len(node.args) < 2:
        return None
    owner = _dotted(node.args[0], bound)
    if owner is None:
        return None
    name = node.args[1]
    if isinstance(name, ast.Constant) and isinstance(name.value, str):
        return f"{owner}.{name.value}"
    return f"{owner}.<dynamic>"


class _Scanner(ast.NodeVisitor):
    """Walks *tree* scope by scope, checking each statement against the aliases in force."""

    def __init__(self, module: dict[str, str], emit: _Emit) -> None:
        self._module = module
        self._emit = emit
        # (aliases in force, whether the frame is a class body). A class body
        # is not an enclosing scope for the functions defined in it.
        self._frames: list[tuple[dict[str, str], bool]] = [(module, False)]

    @property
    def _bound(self) -> dict[str, str]:
        return self._frames[-1][0]

    def _enclosing(self) -> dict[str, str]:
        return next(b for b, is_class in reversed(self._frames) if not is_class)

    def _in_scope(
        self, names: _ScopeNames, children: list[ast.AST], *, is_class: bool = False
    ) -> None:
        self._frames.append(
            (_bindings(names, self._enclosing(), self._module), is_class)
        )
        try:
            for child in children:
                self.visit(child)
        finally:
            self._frames.pop()

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            path = _setattr_path(node, self._bound)
            if path is not None:
                self._emit(node, path)
        for target in _targets(node):
            if isinstance(target, ast.Attribute):
                path = _dotted(target, self._bound)
                if path is not None:
                    self._emit(node, path)
        super().visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for outer in [
            *node.decorator_list,
            *node.args.defaults,
            *(d for d in node.args.kw_defaults if d is not None),
        ]:
            self.visit(outer)
        self._in_scope(_scope_names(list(node.body), node.args), list(node.body))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for outer in [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]:
            self.visit(outer)
        self._in_scope(_scope_names([node.body], node.args), [node.body])

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for outer in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(outer)
        self._in_scope(
            _scope_names(list(node.body), None), list(node.body), is_class=True
        )

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp
    ) -> None:
        first, *rest = node.generators
        self.visit(first.iter)
        names = _ScopeNames()
        for generator in node.generators:
            for target in _targets(generator):
                if isinstance(target, ast.Name):
                    names.other.add(target.id)
                elif isinstance(target, ast.Attribute):
                    # An attribute target assigns through the enclosing scope's
                    # alias; the comprehension binds no name for it.
                    path = _dotted(target, self._bound)
                    if path is not None:
                        self._emit(target, path)
        elements: list[ast.AST] = (
            [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        )
        inner: list[ast.AST] = [first.target, *first.ifs]
        for generator in rest:
            inner.extend([generator.target, generator.iter, *generator.ifs])
        self._frames.append((_bindings(names, self._bound, self._module), False))
        try:
            for child in [*inner, *elements]:
                self.visit(child)
        finally:
            self._frames.pop()

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension


def _message(path: str) -> str:
    replacement = _REPLACEMENTS.get(path)
    fix = (
        f"Instead, {replacement}."
        if replacement
        else (
            "Use the SDK's public extension point for this behaviour instead; if "
            "there is none, that is a gap worth raising with the SDK rather than "
            "patching around."
        )
    )
    return (
        f"`{path}` is rebound at runtime. The SDK reads its own module globals "
        "and class attributes on the assumption that it set them: a refactor "
        "that stops reading this one breaks the app silently, and the patch "
        "reaches only processes that import the patched module (FND-4688). " + fix
    )


def scan_sdk_global_rebind(
    tree: ast.Module,
    file: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Return B010 findings for every rebind of an SDK-imported attribute in *tree*."""
    module = _bindings(_scope_names(list(tree.body), None), {}, {})
    findings: list[Finding] = []
    reported: set[tuple[int, str]] = set()

    def emit(node: ast.AST, path: str) -> None:
        key = (getattr(node, "lineno", 0), path)
        if key in reported:
            return
        reported.add(key)
        findings.append(
            make_finding(
                filename=file,
                rule_id=_RULE_ID,
                node=node,
                message=_message(path),
                directives=directives,
            )
        )

    _Scanner(module, emit).visit(tree)
    return findings
