"""Scope-aware resolution of the names a call is made through.

A file-wide import map answers "was ``h`` ever imported as ``httpx``?", not
"is ``h`` the httpx module *at this call*?".  Python bindings are scope-local:
after ``import httpx as h``, a function whose own parameter is called ``h`` sees
the parameter, not the module.  A rule that matches calls through a file-wide
alias set therefore flags calls on unrelated objects that merely share the name.

This module resolves a name the way Python does — innermost function scope
outward to the module, skipping class bodies for anything nested inside them —
and lets each rule say what the names its imports bind *are* through an
:data:`ImportClassifier`.  Every other binding (parameter, assignment, loop /
``with`` / ``except`` target, nested ``def`` / ``class``) is :data:`UNTRUSTED`,
and so is a name one scope binds two different ways: statically it may be
either at the call.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import TypeVar

#: The kind of every binding that is not a recognised import.
UNTRUSTED = "other"

#: What each name an import statement binds is, as one rule sees it.  Names the
#: rule does not care about map to :data:`UNTRUSTED` (they still shadow).
ImportClassifier = Callable[[ast.Import | ast.ImportFrom], dict[str, str]]

#: Resolves a bare name at the call being matched to its binding kind, or
#: ``None`` when no enclosing scope binds it (a builtin or an undefined name).
NameResolver = Callable[[str], str | None]

_T = TypeVar("_T")

_ScopeNode = (
    ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
)


def _default_exprs(args: ast.arguments) -> list[ast.expr]:
    """The default expressions of *args*.

    ``kw_defaults`` holds ``None`` for each required keyword-only parameter
    (``def f(*, x)``); those are not expressions and are skipped.
    """
    return [*args.defaults, *(d for d in args.kw_defaults if d is not None)]


def scope_bindings(
    scope: _ScopeNode, classify_import: ImportClassifier
) -> dict[str, str]:
    """Every name *scope* binds itself, mapped to its binding kind.

    Parameters, assignments, loop / ``with`` / ``except`` targets, nested
    ``def`` / ``class`` names and imports all bind in the scope that contains
    them (a nested function's *body* is its own scope and is not walked).  A
    name bound two different ways in the same scope is :data:`UNTRUSTED`.
    ``global`` / ``nonlocal`` names bind nothing here.
    """
    bindings: dict[str, str] = {}
    declared_outer: set[str] = set()

    def bind(name: str, kind: str) -> None:
        previous = bindings.get(name)
        bindings[name] = kind if previous in (None, kind) else UNTRUSTED

    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            bind(arg.arg, UNTRUSTED)
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                bind(arg.arg, UNTRUSTED)
    roots: list[ast.AST] = (
        [scope.body] if isinstance(scope, ast.Lambda) else list(scope.body)
    )
    stack = list(roots)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name, UNTRUSTED)
            # Decorators and defaults evaluate in this scope; the body does not.
            stack.extend(node.decorator_list)
            if not isinstance(node, ast.ClassDef):
                stack.extend(_default_exprs(node.args))
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(_default_exprs(node.args))
            continue
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared_outer.update(node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for name, kind in classify_import(node).items():
                bind(name, kind)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bind(node.id, UNTRUSTED)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bind(node.name, UNTRUSTED)
        stack.extend(child for child in ast.iter_child_nodes(node) if child is not None)
    for name in declared_outer:
        bindings.pop(name, None)
    return bindings


def attribute_root(node: ast.expr) -> str | None:
    """The bare name an attribute chain starts from (``a`` in ``a.b.c``)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class _ScopedCallFinder(ast.NodeVisitor):
    """Walk *tree*, handing each call to *match* with a resolver for its scope."""

    def __init__(
        self,
        classify_import: ImportClassifier,
        match: Callable[[ast.Call, NameResolver], _T | None],
    ) -> None:
        self._classify_import = classify_import
        self._match = match
        self._stack: list[tuple[dict[str, str], bool]] = []
        self.matches: list[tuple[ast.Call, _T]] = []

    def _resolve(self, name: str) -> str | None:
        innermost = True
        for bindings, is_class in reversed(self._stack):
            if is_class and not innermost:
                continue
            if name in bindings:
                return bindings[name]
            innermost = False
        return None

    def _visit_scope(self, node: _ScopeNode) -> None:
        self._stack.append(
            (
                scope_bindings(node, self._classify_import),
                isinstance(node, ast.ClassDef),
            )
        )
        self.generic_visit(node)
        self._stack.pop()

    visit_Module = _visit_scope
    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_Lambda = _visit_scope
    visit_ClassDef = _visit_scope

    def visit_Call(self, node: ast.Call) -> None:
        result = self._match(node, self._resolve)
        if result is not None:
            self.matches.append((node, result))
        self.generic_visit(node)


def scoped_calls(
    tree: ast.AST,
    classify_import: ImportClassifier,
    match: Callable[[ast.Call, NameResolver], _T | None],
) -> list[tuple[ast.Call, _T]]:
    """Every call in *tree* that *match* accepts, with what it returned.

    *match* receives the call and a resolver bound to the call's own scope, so
    ``resolve("h")`` is the kind of whatever ``h`` is where the call is made.
    """
    finder = _ScopedCallFinder(classify_import, match)
    finder.visit(tree)
    return finder.matches
