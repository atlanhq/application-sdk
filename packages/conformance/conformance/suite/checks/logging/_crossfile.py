"""Cross-file helpers for L002 (NonCanonicalLoggerFactory) and L016 (BasicConfigNoop).

These functions require the full set of parsed module trees and are called from
``scan_all`` in ``__init__.py``; they are extracted here so ``__init__.py`` stays
as pure public-API orchestration.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._constants import FACTORY_PATTERNS, LOG_METHODS
from ._helpers import main_block_lines


def _detect_factory(tree: ast.Module) -> str | None:
    """Return the logger factory type for *tree*.

    Returns ``None`` when no logger acquisition is detected — the file does
    not obtain a logger object and should not be checked by L002.

    Factory types (first match wins):
    * ``"sdk_adapter"`` — ``from application_sdk... import get_logger`` (canonical)
    * ``"loguru"``      — ``from loguru import logger`` (direct loguru)
    * ``"structlog"``   — ``structlog.get_logger(...)`` in an assignment
    * ``"stdlib"``      — ``logging.getLogger(...)`` / bare ``getLogger(...)`` in an assignment
    * ``"sdk_adapter"`` — bare ``get_logger(...)`` with no stdlib alias in scope
    """
    # 1. SDK adapter import — most specific, check before anything else
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "application_sdk" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "get_logger":
                        return "sdk_adapter"

    # 2. Loguru direct import (from loguru import logger)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "loguru":
                for alias in node.names:
                    if alias.name == "logger":
                        return "loguru"

    # Collect aliases of stdlib getLogger so that
    # ``from logging import getLogger as get_logger`` is not misclassified
    # as the SDK adapter in the bare-call branch below.
    stdlib_getlogger_aliases = _stdlib_getlogger_aliases(tree)

    # 3–5. Factory calls in assignments — use FACTORY_PATTERNS for attr-calls
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        rhs: ast.expr | None = node.value
        if rhs is None or not isinstance(rhs, ast.Call):
            continue
        func = rhs.func
        # Attribute calls: mod.attr(...) matched against FACTORY_PATTERNS
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            for mod, attr, factory in FACTORY_PATTERNS:
                if mod and func.value.id == mod and func.attr == attr:
                    return factory
        # Bare name calls
        elif isinstance(func, ast.Name):
            if func.id == "getLogger":
                return "stdlib"
            if func.id == "get_logger":
                # Aliased from stdlib → treat as stdlib, not SDK adapter
                return (
                    "stdlib" if func.id in stdlib_getlogger_aliases else "sdk_adapter"
                )

    return None


def _stdlib_getlogger_aliases(tree: ast.Module) -> set[str]:
    """Names bound to stdlib ``getLogger`` via ``from logging import getLogger [as X]``."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "logging":
                for alias in node.names:
                    if alias.name == "getLogger":
                        aliases.add(alias.asname or alias.name)
    return aliases


# Methods whose receiver is, by use, a logger emitting records.  ``log`` is
# the level-parameterised form (``logger.log(logging.INFO, ...)``).  Level
# tuning (``setLevel``) and handler wiring (``addHandler``, ``propagate``) are
# deliberately absent: configuring a third-party library's logger is not
# obtaining a logger for the module's own records.
_EMIT_METHODS: frozenset[str] = LOG_METHODS | {"log"}


_FuncScope = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


class _ScopedLoggerUse(ast.NodeVisitor):
    """Collect non-SDK factory binds and log-emitting receivers, keyed by scope.

    A bare-name target/receiver is keyed by the scope that owns the name: the
    innermost enclosing function that assigns it locally (and does not declare
    it ``global``/``nonlocal``), else the module.  A ``self.<attr>`` style
    target/receiver is keyed by the innermost enclosing class, else the module.
    This keeps a function-local ``logger = logging.getLogger("httpx")`` used
    only for ``setLevel`` from matching a module-level ``logger.info(...)``.
    """

    def __init__(self, tree: ast.Module, stdlib_aliases: set[str]) -> None:
        self._module = tree
        self._stdlib_aliases = stdlib_aliases
        self._funcs: list[ast.AST] = []  # function scope stack
        self._classes: list[ast.ClassDef] = []
        self._locals: dict[int, set[str]] = {}
        self.binds: list[tuple[tuple[int, str], ast.stmt, str, str]] = []
        self.receivers: set[tuple[int, str]] = set()

    # -- scope helpers --------------------------------------------------
    def _local_names(self, func: ast.AST) -> set[str]:
        key = id(func)
        if key not in self._locals:
            assigned: set[str] = set()
            declared: set[str] = set()
            args: ast.arguments = func.args  # type: ignore[attr-defined]
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                assigned.add(a.arg)
            for extra in (args.vararg, args.kwarg):
                if extra is not None:
                    assigned.add(extra.arg)
            stack = list(ast.iter_child_nodes(func))
            while stack:
                n = stack.pop()
                if isinstance(n, (*_FuncScope, ast.ClassDef)):
                    if isinstance(
                        n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        assigned.add(n.name)
                    continue  # nested scopes own their own names
                if isinstance(n, (ast.Global, ast.Nonlocal)):
                    declared.update(n.names)
                elif isinstance(n, ast.Name) and isinstance(
                    n.ctx, (ast.Store, ast.Del)
                ):
                    assigned.add(n.id)
                stack.extend(ast.iter_child_nodes(n))
            self._locals[key] = assigned - declared
        return self._locals[key]

    def _key(self, expr: ast.expr) -> tuple[int, str]:
        text = ast.unparse(expr)
        if isinstance(expr, ast.Name):
            for func in reversed(self._funcs):
                if expr.id in self._local_names(func):
                    return (id(func), text)
            return (id(self._module), text)
        owner = self._classes[-1] if self._classes else self._module
        return (id(owner), text)

    # -- visitors -------------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_func(node)

    def _visit_func(self, node: ast.AST) -> None:
        self._funcs.append(node)
        self.generic_visit(node)
        self._funcs.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._classes.append(node)
        self.generic_visit(node)
        self._classes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _EMIT_METHODS:
            if isinstance(func.value, (ast.Name, ast.Attribute)):
                self.receivers.add(self._key(func.value))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._record_bind(node, node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._record_bind(node, [node.target], node.value)
        self.generic_visit(node)

    def _record_bind(
        self, node: ast.stmt, targets: list[ast.expr], rhs: ast.expr | None
    ) -> None:
        if not isinstance(rhs, ast.Call):
            return
        func = rhs.func
        factory: str | None = None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            for mod, attr, kind in FACTORY_PATTERNS:
                if mod and func.value.id == mod and func.attr == attr:
                    factory = kind
                    break
        elif isinstance(func, ast.Name) and func.id in self._stdlib_aliases:
            factory = "stdlib"
        if factory is None:
            return
        for target in targets:
            if isinstance(target, (ast.Name, ast.Attribute)):
                self.binds.append(
                    (self._key(target), node, factory, ast.unparse(target))
                )


def _find_non_sdk_logger_binds(tree: ast.Module) -> list[tuple[ast.stmt, str, str]]:
    """Return non-SDK logger binds in a file that also imports the SDK adapter.

    ``_detect_factory`` classifies any file importing the SDK ``get_logger`` as
    ``"sdk_adapter"`` (first match wins), so a file that imports the adapter
    but still binds its own logger through ``logging.getLogger`` /
    ``structlog.get_logger`` would otherwise pass L002.

    A *bind* is an assignment whose right-hand side is a stdlib or structlog
    factory call.  It is reported only when the same binding — same name in
    the same scope, or same ``self.<attr>`` in the same class — is the
    receiver of a log-emitting call (``<target>.info(...)`` etc.), so
    ``logging.getLogger("httpx").setLevel(...)`` and
    ``h = logging.getLogger("httpx"); h.setLevel(...)`` — third-party level
    tuning — are not flagged, even next to a module ``logger`` that is.

    Returns ``(assignment_node, factory_type, target_text)`` per flagged bind.
    """
    visitor = _ScopedLoggerUse(tree, _stdlib_getlogger_aliases(tree) | {"getLogger"})
    visitor.visit(tree)
    return [
        (node, factory, text)
        for key, node, factory, text in visitor.binds
        if key in visitor.receivers
    ]


def _collect_basicconfig_calls(
    tree: ast.Module, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Collect placeholder findings for every ``logging.basicConfig()`` call.

    The caller (``scan_all``) promotes the 2nd+ non-main calls to real L016
    findings.  We record position here and filter in the cross-file pass.

    Only ``logging.basicConfig()`` and its aliases (``import logging as L;
    L.basicConfig()``) are collected.  Third-party methods that happen to be
    named ``basicConfig`` (e.g. ``mock.basicConfig()``) are excluded by
    restricting the Attribute-call branch to receivers whose name is bound to
    the ``logging`` module.
    """
    # Collect names bound to 'import logging [as X]' to avoid false-positives
    logging_names: set[str] = {"logging"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "logging":
                    logging_names.add(alias.asname or alias.name.split(".")[0])

    main_lines = main_block_lines(tree)
    calls: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_basicconfig = (
            isinstance(func, ast.Attribute)
            and func.attr == "basicConfig"
            and isinstance(func.value, ast.Name)
            and func.value.id in logging_names
        ) or (isinstance(func, ast.Name) and func.id == "basicConfig")
        if not is_basicconfig:
            continue
        lineno = getattr(node, "lineno", 1)
        if lineno in main_lines:
            continue  # inside __main__ block — exempt
        calls.append(
            make_finding(
                filename=filename,
                rule_id="L016",
                node=node,
                message=(
                    "Multiple logging.basicConfig() calls — second and later calls "
                    "are silent no-ops (basicConfig() does nothing when the root "
                    "logger already has handlers). Consolidate into a single call."
                ),
                directives=directives,
            )
        )
    return calls
