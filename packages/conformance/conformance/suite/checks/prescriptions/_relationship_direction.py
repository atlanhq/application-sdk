"""P055 OneToManyLinkFromParent — a mapper populates the list end of a 1-to-N link.

Publish orders entities by type using the typedef relationships.  In a 1-to-N
relationship it treats the "1" side as the parent and sends it first (Table
before Column); the "N" side carries the reference back to its single parent
(``Column.table``), so the parent always exists by the time that reference
arrives.  A mapper that writes the link the other way — populating the list end
on the parent (``Table.columns``, ``Process.fabric_activities``) — makes the
parent name children that do not exist yet.  Atlas rejects it with
``ATLAS-404-00-00A`` and the run fails until a later run, once the children
exist (FND-3490).

Per-file, pyatlan_v9 mappers only (a module importing
``pyatlan_v9.model.assets``).  The list ends come from the baked
:mod:`._relationship_directions` table.  Flags, for an owner type ``X`` and one
of its list ends ``a``:

* ``X(..., a=...)`` / ``X.creator(..., a=...)``;
* ``x.a = ...`` / ``x.a += ...`` and ``x.a.append(...)`` / ``.extend(...)`` /
  ``.insert(...)``, where ``x`` is bound in the same scope to an ``X(...)`` or
  ``X.creator(...)`` call, or annotated ``X`` (as a variable or a parameter).

When the receiver's type is not known (``self.process.a = ...``, a value from a
helper), the site is still flagged if the value itself names the "N" type —
``[RelatedFabricActivity(...)]`` or ``FabricActivity.ref_by_qualified_name(...)``
— that pairs with ``a`` as a list end, and no pyatlan_v9 type carries a list
``a`` that is not a 1-to-N list end.  Anything else is left alone, so an
unresolved receiver is a missed finding rather than a false one.  Assigning
``None`` or an empty list is not flagged: it names no children.

Names resolve lexically: a pyatlan_v9 import name (``Table``, a module alias
``A``) that a scope or an enclosing function rebinds — a parameter, an
assignment, another import — is not treated as pyatlan_v9 there.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._relationship_directions import SetEnd, load_relationship_data

RULE_ID = "P055"

_ASSETS_MODULE = "pyatlan_v9.model.assets"
_RELATED_PREFIX = "Related"
#: Classmethods on an asset class that return an instance of that class.
_FACTORY_METHODS = frozenset({"creator", "updater"})
#: Classmethods on an asset class that build a reference to that type.
_REF_METHODS = frozenset({"ref_by_qualified_name", "ref_by_guid"})
_LIST_MUTATORS = frozenset({"append", "extend", "insert"})
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _is_assets_module(module: str | None) -> bool:
    return module is not None and (
        module == _ASSETS_MODULE or module.startswith(f"{_ASSETS_MODULE}.")
    )


def _collect_bindings(tree: ast.AST) -> tuple[dict[str, str], frozenset[str]]:
    """Return ``(local name -> pyatlan_v9 class name, module aliases)``.

    The class map covers ``from pyatlan_v9.model.assets[.x] import Y [as Z]``;
    the module aliases cover ``import pyatlan_v9.model.assets as A`` and
    ``from pyatlan_v9.model import assets``, whose ``A.Y`` resolves to ``Y``.
    """
    classes: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if _is_assets_module(node.module):
                for alias in node.names:
                    classes[alias.asname or alias.name] = alias.name
            elif node.module == "pyatlan_v9.model":
                for alias in node.names:
                    if alias.name == "assets":
                        modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _ASSETS_MODULE and alias.asname:
                    modules.add(alias.asname)
    return classes, frozenset(modules)


class _Resolver:
    """Resolves expressions to pyatlan_v9 class names within one module."""

    def __init__(self, classes: dict[str, str], modules: frozenset[str]) -> None:
        self._classes = classes
        self._modules = modules

    def without(self, shadowed: frozenset[str]) -> _Resolver:
        """This resolver with the names a scope rebinds removed."""
        if not shadowed & (self._classes.keys() | self._modules):
            return self
        return _Resolver(
            {k: v for k, v in self._classes.items() if k not in shadowed},
            self._modules - shadowed,
        )

    def class_ref(self, node: ast.expr | None) -> str | None:
        """``Y`` for a bare ``Y`` / ``A.Y`` naming an imported pyatlan_v9 class."""
        if isinstance(node, ast.Name):
            return self._classes.get(node.id)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in self._modules
        ):
            return node.attr
        return None

    def constructed(self, node: ast.expr | None) -> str | None:
        """The asset class a ``Y(...)`` / ``Y.creator(...)`` call returns."""
        if not isinstance(node, ast.Call):
            return None
        direct = self.class_ref(node.func)
        if direct is not None:
            return direct
        if isinstance(node.func, ast.Attribute) and node.func.attr in _FACTORY_METHODS:
            return self.class_ref(node.func.value)
        return None

    def referenced_types(self, node: ast.expr) -> set[str]:
        """Asset types the value names as relationship targets.

        ``RelatedY(...)`` and ``Y.ref_by_qualified_name(...)`` / ``ref_by_guid``
        anywhere in the value — a list display, a comprehension, a bare call.
        """
        found: set[str] = set()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            name = self.class_ref(sub.func)
            if name is not None and name.startswith(_RELATED_PREFIX):
                found.add(name[len(_RELATED_PREFIX) :])
            elif isinstance(sub.func, ast.Attribute) and sub.func.attr in _REF_METHODS:
                target = self.class_ref(sub.func.value)
                if target is not None:
                    found.add(target)
        return found


_Body = Sequence[ast.AST]


def _iter_scope(body: _Body) -> Iterator[ast.AST]:
    """Yield nodes executing in this scope.

    A nested def, lambda or class is yielded itself (it binds a name or is a
    value here) but not descended into: its body is a scope of its own.
    """
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _NESTED_SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _params(args: ast.arguments) -> list[ast.arg]:
    return [
        *args.posonlyargs,
        *args.args,
        *([args.vararg] if args.vararg else []),
        *args.kwonlyargs,
        *([args.kwarg] if args.kwarg else []),
    ]


def _is_pyatlan_import(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> bool:
    """Whether this import alias is one :func:`_collect_bindings` records."""
    if isinstance(node, ast.ImportFrom):
        return _is_assets_module(node.module) or (
            node.module == "pyatlan_v9.model" and alias.name == "assets"
        )
    return alias.name == _ASSETS_MODULE and alias.asname is not None


def _rebound_names(body: _Body, params: list[ast.arg]) -> frozenset[str]:
    """Names this scope binds by anything other than a pyatlan_v9 import."""
    names = {param.arg for param in params}
    for node in _iter_scope(body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            names.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                if not _is_pyatlan_import(node, alias):
                    names.add(alias.asname or alias.name.split(".")[0])
    return frozenset(names)


def _scopes(
    tree: ast.Module,
) -> Iterator[tuple[_Body, list[ast.arg], frozenset[str]]]:
    """Every scope's body, its parameters and the names rebound where it runs.

    The module, then each function, lambda and class body.  A function sees
    the names its enclosing functions rebind; a class body's names do not reach
    the methods inside it.
    """

    def walk(
        body: _Body, params: list[ast.arg], outer: frozenset[str], is_class: bool
    ) -> Iterator[tuple[_Body, list[ast.arg], frozenset[str]]]:
        rebound = outer | _rebound_names(body, params)
        yield body, params, rebound
        inherited = outer if is_class else rebound
        for node in _iter_scope(body):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                yield from walk(node.body, _params(node.args), inherited, False)
            elif isinstance(node, ast.Lambda):
                yield from walk([node.body], _params(node.args), inherited, False)
            elif isinstance(node, ast.ClassDef):
                yield from walk(node.body, [], inherited, True)

    yield from walk(tree.body, [], frozenset(), False)


def _local_types(
    body: _Body, params: list[ast.arg], resolver: _Resolver
) -> dict[str, str]:
    """Names bound to a single known asset type in this scope.

    A name bound to more than one type, or also bound to something unknown, is
    left out: which binding reaches a given use is not tracked.
    """
    seen: dict[str, set[str | None]] = {}
    for param in params:
        seen.setdefault(param.arg, set()).add(resolver.class_ref(param.annotation))
    for node in _iter_scope(body):
        if isinstance(node, ast.Assign):
            bound = resolver.constructed(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    seen.setdefault(target.id, set()).add(bound)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound = resolver.class_ref(node.annotation) or resolver.constructed(
                node.value
            )
            seen.setdefault(node.target.id, set()).add(bound)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.With | ast.AsyncWith):
            targets = (
                [node.target]
                if isinstance(node, ast.For | ast.AsyncFor)
                else [item.optional_vars for item in node.items]
            )
            for target in targets:
                if isinstance(target, ast.Name):
                    seen.setdefault(target.id, set()).add(None)
    resolved: dict[str, str] = {}
    for name, types in seen.items():
        (only,) = types if len(types) == 1 else (None,)
        if only is not None:
            resolved[name] = only
    return resolved


def _names_nothing(node: ast.expr | None) -> bool:
    """``None``, ``[]``, ``()`` or ``list()`` — a value that names no children."""
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.List | ast.Tuple):
        return not node.elts
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"list", "tuple"}
        and not node.args
        and not node.keywords
    )


def _message(owner: str | None, field: str, ends: list[SetEnd]) -> str:
    """Name the list end and the child reference to set instead.

    ``owner`` is ``None`` when the site was resolved by value and several owner
    types carry this list end (``links`` on every asset).  ``ends`` holds more
    than one entry when those owners pair it with different child references
    (``fabric_activities`` is ``FabricActivity.fabric_process`` on a
    ``Process`` and ``fabric_data_pipeline`` on a ``FabricDataPipeline``).
    """
    target = ends[0].target_type
    where = f"{owner}.{field}" if owner is not None else f"{field} on the parent"
    parent = owner or "the parent"
    if len(ends) == 1:
        fix = f"set {target}.{ends[0].inverse} on each {target} instead"
    else:
        options = " or ".join(f"{target}.{end.inverse}" for end in ends)
        fix = (
            f"set the single reference on each {target} instead ({options}, "
            f"whichever names this parent)"
        )
    return (
        f"{where} is the list end of a 1-to-N relationship — {fix}. Publish "
        f"sends {parent} first, so the {target} entities it names do not exist "
        f"yet and Atlas rejects it with ATLAS-404-00-00A."
    )


class _P055:
    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        resolver: _Resolver,
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._resolver = resolver
        data = load_relationship_data()
        self._table = data.set_ends
        self._other_list_fields = data.other_list_fields
        self.findings: list[Finding] = []
        self._flagged: set[tuple[int, int]] = set()

    def _emit(
        self, node: ast.AST, owner: str | None, field: str, ends: list[SetEnd]
    ) -> None:
        key = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        if key in self._flagged:
            return
        self._flagged.add(key)
        self.findings.append(
            make_finding(
                filename=self._filename,
                rule_id=RULE_ID,
                node=node,
                message=_message(owner, field, ends),
                directives=self._directives,
            )
        )

    def _by_value(
        self, field: str, value: ast.expr
    ) -> tuple[str | None, list[SetEnd]] | None:
        """``(owner, ends)`` for an unresolved receiver, judged by what it holds.

        The value must name exactly one "N" type, and ``field`` must be a 1-to-N
        list end holding that type on at least one owner and never a list of
        another kind (``reports`` is many-to-many on ``SalesforceDashboard``).
        The owner is named only when it is the only one.
        """
        if field in self._other_list_fields:
            return None
        targets = self._resolver.referenced_types(value)
        if len(targets) != 1:
            return None
        (target,) = targets
        matches = {
            (owner, end)
            for owner, fields in self._table.items()
            if (end := fields.get(field)) is not None and end.target_type == target
        }
        if not matches:
            return None
        owners = {owner for owner, _ in matches}
        ends = sorted({end for _, end in matches}, key=lambda end: end.inverse)
        return (next(iter(owners)) if len(owners) == 1 else None), ends

    def _check_attribute(
        self,
        site: ast.AST,
        receiver: ast.expr,
        field: str,
        value: ast.expr | None,
        local_types: dict[str, str],
    ) -> None:
        if value is None or _names_nothing(value):
            return
        owner = local_types.get(receiver.id) if isinstance(receiver, ast.Name) else None
        if owner is not None:
            end = self._table.get(owner, {}).get(field)
            if end is not None:
                self._emit(site, owner, field, [end])
            return
        resolved = self._by_value(field, value)
        if resolved is not None:
            self._emit(site, resolved[0], field, resolved[1])

    def scan_scope(
        self, body: _Body, params: list[ast.arg], resolver: _Resolver
    ) -> None:
        self._resolver = resolver
        local_types = _local_types(body, params, resolver)
        for node in _iter_scope(body):
            if isinstance(node, ast.Call):
                self._check_call(node, local_types)
            elif isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Attribute):
                        self._check_attribute(
                            node, target.value, target.attr, node.value, local_types
                        )

    def _check_call(self, node: ast.Call, local_types: dict[str, str]) -> None:
        owner = self._resolver.constructed(node)
        if owner is not None:
            fields = self._table.get(owner, {})
            for kw in node.keywords:
                if (
                    kw.arg is not None
                    and kw.arg in fields
                    and not _names_nothing(kw.value)
                ):
                    self._emit(node, owner, kw.arg, [fields[kw.arg]])
            return
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _LIST_MUTATORS
            and isinstance(func.value, ast.Attribute)
            and node.args
        ):
            self._check_attribute(
                node, func.value.value, func.value.attr, node.args[-1], local_types
            )


def check_p055(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P055 for a pyatlan_v9 mapper populating the list end of a 1-to-N link."""
    if not isinstance(tree, ast.Module):
        return []
    classes, modules = _collect_bindings(tree)
    if not classes and not modules:
        return []
    resolver = _Resolver(classes, modules)
    checker = _P055(filename, directives, resolver)
    for body, params, rebound in _scopes(tree):
        checker.scan_scope(body, params, resolver.without(rebound))
    return checker.findings
