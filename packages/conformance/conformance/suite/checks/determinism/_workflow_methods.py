"""Shared discovery for the determinism / async-correctness rules (P020–P024).

Determinism rules apply **only** to code that runs in the Temporal *workflow replay
context* — never to ``@task`` activity bodies (which run once and may do I/O,
sleep, randomness).  The SDK draws this line statically and unambiguously on every
``App`` subclass (``application_sdk/app/base.py``):

* ``async def run`` → becomes ``@workflow.run`` (workflow context).
* ``@entrypoint`` methods → one Temporal workflow each (workflow context).
* ``@signal`` / ``@query`` / ``@update`` methods → relayed handlers (workflow context).
* ``@task`` methods → Temporal activities (run-once; **never** flagged).
* undecorated helpers → ambiguous (call-graph dependent); a documented
  false-negative — see the module docstring of :mod:`...determinism`.

This module provides the two load-bearing primitives the detectors share:

* :func:`workflow_method_nodes` — the precise set of workflow-context function
  nodes in a module, classified alias-aware via the shared decorator-provenance
  helpers (so ``@task as t`` / ``@app.task`` are still recognised as activities).
* :func:`resolve_call_target` — receiver-anchored resolution of a call's callable
  to a fully-qualified dotted origin (``datetime.datetime.now``, ``time.time``,
  ``random.randint``), so a detector matches the *real* target rather than a bare
  method name.  This is what leaves the sanctioned ``self.now()`` / ``self.uuid()``
  / ``application_sdk.app.now`` untouched.
"""

from __future__ import annotations

import ast

from conformance.suite.checks.prescriptions._decorator_provenance import (
    ImportProvenance,
    collect_import_provenance,
    is_entrypoint_decorator,
    is_interaction_decorator,
    is_task_decorator,
)

_SDK_PREFIX = "application_sdk"


# ── App-subclass discovery ───────────────────────────────────────────────────


def _sdk_app_aliases(tree: ast.AST) -> frozenset[str]:
    """Return local names bound to the SDK ``App`` class in this module.

    Mirrors ``entrypoint_alignment._code_entrypoints._sdk_app_aliases`` so a
    ``from application_sdk.app import App as Base`` import still anchors discovery.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level > 0:
            continue
        module = node.module or ""
        if module == _SDK_PREFIX or module.startswith(_SDK_PREFIX + "."):
            for alias in node.names:
                if alias.name == "App":
                    bound.add(alias.asname or alias.name)
    return frozenset(bound)


def _is_app_subclass(node: ast.ClassDef, app_aliases: frozenset[str]) -> bool:
    """True if *node* directly subclasses the SDK ``App`` class (by alias name)."""
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in app_aliases:
            return True
        if isinstance(base, ast.Attribute) and base.attr in app_aliases:
            return True
    return False


def _is_workflow_method(
    method: ast.FunctionDef | ast.AsyncFunctionDef, prov: ImportProvenance
) -> bool:
    """Classify a method on an ``App`` subclass as workflow-context or not.

    ``@task`` (activity) always wins — a method decorated with both ``@task`` and
    anything else is an activity and must never be flagged.  Otherwise a method is
    workflow-context when it is ``run`` or carries ``@entrypoint`` /
    ``@signal`` / ``@query`` / ``@update``.
    """
    decorators = method.decorator_list
    if any(is_task_decorator(dec, prov) for dec in decorators):
        return False
    if method.name == "run":
        return True
    return any(
        is_entrypoint_decorator(dec, prov) or is_interaction_decorator(dec, prov)
        for dec in decorators
    )


def workflow_method_nodes(
    tree: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every workflow-context method defined on an ``App`` subclass.

    Only methods declared *directly* in an ``App`` subclass body are classified;
    nested functions inside a workflow method are covered transitively because the
    detectors ``ast.walk`` each returned node.  Returns ``[]`` when the module does
    not import the SDK ``App`` (nothing to anchor on).
    """
    app_aliases = _sdk_app_aliases(tree)
    if not app_aliases:
        return []
    prov = collect_import_provenance(tree)
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and _is_app_subclass(node, app_aliases)):
            continue
        for member in node.body:
            if isinstance(
                member, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and _is_workflow_method(member, prov):
                methods.append(member)
    return methods


def async_method_names(tree: ast.AST) -> frozenset[str]:
    """Return the names of every ``async def`` method across all ``App`` subclasses.

    Used by P022 to recognise a bare ``self.<name>(...)`` call as a dropped
    coroutine: any ``async def`` method (a ``@task``, ``run``, an ``@entrypoint``,
    or a plain async helper) returns a coroutine, so calling it without ``await``
    (and not wrapping it in ``create_task``/``gather``) silently drops the work.
    """
    app_aliases = _sdk_app_aliases(tree)
    if not app_aliases:
        return frozenset()
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and _is_app_subclass(node, app_aliases)):
            continue
        for member in node.body:
            if isinstance(member, ast.AsyncFunctionDef):
                names.add(member.name)
    return frozenset(names)


# ── Receiver-anchored call-target resolution ─────────────────────────────────


def resolve_call_target(func: ast.expr, bindings: dict[str, str]) -> str | None:
    """Resolve a call's callable expression to a fully-qualified dotted origin.

    Walks an attribute chain down to its root ``Name`` and rewrites that root via
    the module's import bindings (``collect_import_bindings``):

    * ``import datetime;            datetime.datetime.now()`` → ``datetime.datetime.now``
    * ``from datetime import datetime; datetime.now()``       → ``datetime.datetime.now``
    * ``import time;                time.time()``             → ``time.time``
    * ``from time import time;      time()``                  → ``time.time``
    * ``self.now()``                                          → ``self.now`` (unbound root)
    * ``open(...)``                                           → ``open``

    Returns ``None`` when the root is not a plain ``Name`` (e.g. a call on a
    subscript or another call result) — such targets cannot be matched reliably.
    """
    attrs: list[str] = []
    cur: ast.expr = func
    while isinstance(cur, ast.Attribute):
        attrs.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    attrs.append(cur.id)
    attrs.reverse()  # [root, attr1, attr2, ...]
    root = attrs[0]
    origin = bindings.get(root)
    if origin is None:
        # Unbound root: a local variable, ``self``, or a builtin (``open``).
        return ".".join(attrs)
    return ".".join([origin, *attrs[1:]])
