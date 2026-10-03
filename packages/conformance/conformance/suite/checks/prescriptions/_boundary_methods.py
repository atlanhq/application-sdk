"""Classify a class-body method as an ``@entrypoint``, a ``@task``, or an implicit ``App.run()``.

The one place the scans that read entrypoint contracts (K006/K016/K018/K021,
B005/B006 and the contract ledger, F004/F019, P013/P014) decide which methods
are workflow boundaries. The runtime ground truth is
``application_sdk.app._ep_registration._collect_implicit_ep`` together with
``App.__init_subclass__``: an undecorated ``async def run`` is an entrypoint of
any class that (transitively) subclasses ``App``.

A base counts as ``App``-family when it is

* the name ``App``;
* a name the module binds to an SDK ``App``-family class by an absolute
  ``from application_sdk[.<sub>] import <X>`` (see
  :func:`~conformance.suite.checks._ast_common.sdk_app_base_bindings`); or
* an in-repo class, in any scanned file, whose own bases reach one of those.

The SDK templates live outside the scanned repo, so the last case relies on
:attr:`ClassRecord.sdk_app_bases`, which records the import provenance of each
base in the file that defines the class.

:func:`inherited_run_base` answers the complementary question for a class
with no ``run`` of its own: which SDK ``App``-family base its ``run`` comes
from, which K018 maps to that template's ``run()`` input.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NamedTuple

from conformance.suite.checks._ast_common import sdk_app_base_bindings

from ._decorator_provenance import (
    ImportProvenance,
    is_entrypoint_decorator,
    is_task_decorator,
)
from ._error_code_prefix import ClassRecord

BoundaryKind = Literal["entrypoint", "task", "implicit_run"]


class BoundaryMethod(NamedTuple):
    """How a method is a boundary, plus its ``@entrypoint`` decorator if it has one."""

    kind: BoundaryKind
    decorator: ast.expr | None = None


@dataclass(frozen=True)
class BoundaryScope:
    """Per-module inputs to :func:`classify_boundary_method`.

    *by_name* and *app_cache* are the caller's cross-file class registry and its
    memo of :func:`reaches_app_family` results; a caller that shadows the
    registry for one file passes that file's own cache with it.
    """

    prov: ImportProvenance
    aliases: Mapping[str, str]
    by_name: Mapping[str, ClassRecord]
    app_cache: dict[str, bool | None]
    sdk_bases: frozenset[str]

    @classmethod
    def for_module(
        cls,
        tree: ast.AST,
        *,
        prov: ImportProvenance,
        aliases: Mapping[str, str],
        by_name: Mapping[str, ClassRecord],
        app_cache: dict[str, bool | None],
    ) -> BoundaryScope:
        return cls(
            prov=prov,
            aliases=aliases,
            by_name=by_name,
            app_cache=app_cache,
            sdk_bases=sdk_app_base_bindings(tree),
        )


def classify_boundary_method(
    class_node: ast.ClassDef,
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    scope: BoundaryScope,
) -> BoundaryMethod | None:
    """Return how *func* (defined on *class_node*) is a boundary, or ``None``.

    An SDK ``@entrypoint`` wins over an SDK ``@task`` on the same method; a
    ``@task``-marked method is never an implicit ``run()`` entrypoint.
    """
    for dec in func.decorator_list:
        if is_entrypoint_decorator(dec, scope.prov):
            return BoundaryMethod("entrypoint", dec)
    if any(is_task_decorator(dec, scope.prov) for dec in func.decorator_list):
        return BoundaryMethod("task")
    if (
        func.name == "run"
        and isinstance(func, ast.AsyncFunctionDef)
        and is_app_family_class(class_node, scope)
    ):
        return BoundaryMethod("implicit_run")
    return None


def is_app_family_class(class_node: ast.ClassDef, scope: BoundaryScope) -> bool:
    """True if a base of *class_node* is, or transitively reaches, an ``App``-family class."""
    for base in class_node.bases:
        if isinstance(base, ast.Name):
            if base.id in scope.sdk_bases:
                return True
            name = base.id
        elif isinstance(base, ast.Attribute):
            name = base.attr
        else:
            continue
        name = scope.aliases.get(name, name)
        if (
            name == "App"
            or reaches_app_family(name, scope.by_name, scope.app_cache, set()) is True
        ):
            return True
    return False


def reaches_app_family(
    name: str,
    by_name: Mapping[str, ClassRecord],
    cache: dict[str, bool | None],
    visiting: set[str],
) -> bool | None:
    """Resolve whether the in-repo class *name* reaches an ``App``-family base.

    Same ``True``/``False``/``None`` contract, memoisation and cycle handling as
    ``resolve_ancestor(name, "App", ...)``, with one extra stop: a base that the
    class's own module imported from the SDK ``App`` family is ``True``.
    """
    if name == "App":
        return True
    if name in cache:
        return cache[name]
    if name in visiting:
        return None
    rec = by_name.get(name)
    if rec is None:
        cache[name] = None
        return None
    visiting.add(name)
    result = False
    same_name_base = False
    for base in rec.bases:
        if base in rec.sdk_app_bases:
            result = True
            break
        if base == name:
            same_name_base = True
            continue
        if reaches_app_family(base, by_name, cache, visiting) is True:
            result = True
            break
    visiting.discard(name)
    if not result and same_name_base:
        cache[name] = None
        return None
    cache[name] = result
    return result


def inherited_run_base(name: str, by_name: Mapping[str, ClassRecord]) -> str | None:
    """The SDK ``App``-family base whose ``run`` the in-repo class *name* inherits.

    Mirrors the ``cls.run`` lookup ``_collect_implicit_ep`` does for a class
    with no ``run`` of its own: bases are searched depth-first in declaration
    order, and the first SDK ``App``-family base reached (per
    :attr:`ClassRecord.sdk_app_bases`) is returned by its SDK name. ``None``
    when *name* or an in-repo base on the way defines ``run`` itself, when a
    base cannot be resolved (it might define ``run``), or when no base reaches
    the SDK ``App`` family.
    """
    found = _run_owner(name, by_name, set())
    return found if isinstance(found, str) else None


def _run_owner(
    name: str, by_name: Mapping[str, ClassRecord], visiting: set[str]
) -> str | bool | None:
    """SDK base name, ``False`` for a branch with no ``App`` base, ``None`` to stop."""
    rec = by_name.get(name)
    if rec is None or name in visiting or _defines_run(rec.node):
        return None
    visiting.add(name)
    try:
        for base in rec.bases:
            if base in rec.sdk_app_bases:
                return base
            if base == name:
                return None
            found = _run_owner(base, by_name, visiting)
            if found is not False:
                return found
        return False
    finally:
        visiting.discard(name)


def _defines_run(class_node: ast.ClassDef) -> bool:
    for item in class_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name == "run":
                return True
        elif isinstance(item, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "run" for t in item.targets):
                return True
        elif isinstance(item, ast.AnnAssign):
            if isinstance(item.target, ast.Name) and item.target.id == "run":
                return True
    return False
