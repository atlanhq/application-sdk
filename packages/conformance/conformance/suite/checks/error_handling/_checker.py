"""Checker — assembles rule mixins and drives the AST visitor dispatch."""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from .dependency_audience import DependencyAudienceMixin
from .exception_chaining import ExceptionChainingMixin, ExceptionHelper
from .http_failure import HttpFailureMixin
from .security import SecurityMixin
from .silent_swallow import SilentSwallowMixin
from .untyped_raise import UntypedRaiseMixin, module_bindings


def import_bindings(tree: ast.Module) -> dict[str, str]:
    """Each name an import binds, mapped to the module or object it names.

    ``import a.b`` binds ``a`` to the package ``a`` (``a.b`` is reached through
    it), unlike ``import a.b as c``, which binds ``c`` to ``a.b``. A name the
    module also assigns, defines or rebinds anywhere is left out: after
    ``asyncio = domain_errors`` the name no longer names the stdlib module.
    Likewise a name imported from different origins (e.g. a function-local
    import shadowing a module-level one) is left out: the map is module-wide
    and cannot say which binding a given use sees.
    """
    bound: dict[str, str] = {}
    conflicting: set[str] = set()

    def _bind(name: str, origin: str) -> None:
        if bound.setdefault(name, origin) != origin:
            conflicting.add(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    _bind(alias.asname, alias.name)
                else:
                    root = alias.name.split(".")[0]
                    _bind(root, root)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                _bind(alias.asname or alias.name, f"{node.module}.{alias.name}")
    rebound = (
        conflicting
        | {
            n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store | ast.Del)
        }
        | {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }
    )
    return {name: origin for name, origin in bound.items() if name not in rebound}


class Checker(
    SilentSwallowMixin,
    UntypedRaiseMixin,
    ExceptionChainingMixin,
    HttpFailureMixin,
    SecurityMixin,
    DependencyAudienceMixin,
    ast.NodeVisitor,
):
    """Walk a module AST and emit E-series findings."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        atlan_ioerror_imported: bool,
        legacy_aliases: frozenset[str] = frozenset(),
        local_helpers: dict[str, ast.FunctionDef] | None = None,
        sdk_errors: frozenset[str] = frozenset(),
        raised_error_helpers: dict[str, ExceptionHelper] | None = None,
        dependency_classes: frozenset[str] = frozenset(),
        deprecated_module: bool = False,
        script_file: bool = False,
        preflight_lines: frozenset[int] = frozenset(),
        comment_lines: frozenset[int] = frozenset(),
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._atlan_ioerror_imported = atlan_ioerror_imported
        self._legacy_aliases = legacy_aliases
        self._local_helpers = local_helpers or {}
        self._sdk_errors = sdk_errors
        self._exception_helpers = raised_error_helpers or {}
        self._dependency_classes = dependency_classes
        self._deprecated_module = deprecated_module
        # A standalone script/CLI (L005's is_script_file): E004 accepts a
        # print() of the caught exception there.
        self._script_file = script_file
        # ``def`` lines of the functions in F005's preflight scope (see
        # preflight._common.preflight_scope_lines): E004 accepts a DEBUG/INFO
        # log carrying the exception there, where F005 bars WARNING.
        self._preflight_lines = preflight_lines
        # Lines holding an explanatory comment (not a directive): E008 accepts
        # one on an optional-import guard, as its own text says.
        self._comment_lines = comment_lines
        self._findings: list[Finding] = []
        self._import_origins: dict[str, str] = {}
        # Context stacks — managed by visit_* methods
        self._function_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self._module: ast.Module | None = None
        self._except_stack: list[ast.ExceptHandler] = []
        self._try_stack: list[ast.Try | ast.TryStar] = []
        # Every name the module binds, and the subset bound by anything other
        # than an import or a module-level class (see module_bindings).
        self._bound_names: frozenset[str] = frozenset()
        self._rebound_names: frozenset[str] = frozenset()
        self._class_stack: list[ast.ClassDef] = []
        self._loop_stack: list[ast.For | ast.AsyncFor | ast.While] = []
        # Set True while generic_visit walks the children of a Raise node so
        # _check_p017_call can skip the inline Call (already covered by
        # _check_p017_raise on the outer Raise).
        self._in_raise_call: bool = False

    # ── Finding creation ──────────────────────────────────────────────────────

    def _add(self, rule_id: str, node: ast.AST, message: str) -> None:
        # Suppression-aware Finding construction is shared with the P/O series;
        # delegate to the neutral helper rather than re-implementing it here.
        self._findings.append(
            make_finding(
                filename=self._filename,
                rule_id=rule_id,
                node=node,
                message=message,
                directives=self._directives,
            )
        )

    # ── Context management ────────────────────────────────────────────────────

    def visit_Module(self, node: ast.Module) -> None:
        self._module = node
        self._import_origins = import_bindings(node)
        self._bound_names, self._rebound_names = module_bindings(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # type: ignore[override]
        # Reset loop/except/try context: a nested function body executes later
        # and is not protected by the surrounding function's handlers.
        saved_loops = self._loop_stack
        saved_excepts = self._except_stack
        saved_tries = self._try_stack
        self._loop_stack = []
        self._except_stack = []
        self._try_stack = []
        self._function_stack.append(node)
        self._check_p010_in_function(node)
        self.generic_visit(node)
        self._function_stack.pop()
        self._loop_stack = saved_loops
        self._except_stack = saved_excepts
        self._try_stack = saved_tries

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # type: ignore[override]
        saved_loops = self._loop_stack
        saved_excepts = self._except_stack
        saved_tries = self._try_stack
        self._loop_stack = []
        self._except_stack = []
        self._try_stack = []
        self._function_stack.append(node)  # type: ignore[arg-type]
        self._check_p010_in_function(node)  # type: ignore[arg-type]
        self.generic_visit(node)
        self._function_stack.pop()
        self._loop_stack = saved_loops
        self._except_stack = saved_excepts
        self._try_stack = saved_tries

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check_p011(node)
        if not self._class_stack and not self._function_stack:
            self._check_e021(node)
        self._class_stack.append(node)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_For(self, node: ast.For) -> None:
        self._loop_stack.append(node)
        self.generic_visit(node)
        self._loop_stack.pop()

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._loop_stack.append(node)  # type: ignore[arg-type]
        self.generic_visit(node)
        self._loop_stack.pop()

    def visit_While(self, node: ast.While) -> None:
        self._loop_stack.append(node)  # type: ignore[arg-type]
        self.generic_visit(node)
        self._loop_stack.pop()

    def visit_Try(self, node: ast.Try | ast.TryStar) -> None:
        # Except clauses catch failures from the try body only, not from their
        # handlers, else suite, or finally suite. Preserve that distinction for
        # E012's internal-sentinel translation check. ``except*`` scopes are
        # tracked too, so an inner except* handler is not skipped in favour of
        # an outer try's handler.
        self._try_stack.append(node)
        for stmt in node.body:
            self.visit(stmt)
        self._try_stack.pop()
        for handler in node.handlers:
            self.visit(handler)
        for stmt in node.orelse:
            self.visit(stmt)
        for stmt in node.finalbody:
            self.visit(stmt)

    visit_TryStar = visit_Try

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._except_stack.append(node)
        self._check_p001_p002_p006(node)
        self._check_p004(node)
        self._check_p005(node)
        self._check_p007(node)
        self._check_p008(node)
        self._check_p009(node)
        self._check_p014(node)
        self.generic_visit(node)
        self._except_stack.pop()

    def visit_Raise(self, node: ast.Raise) -> None:
        self._check_p012(node)
        self._check_p013(node)
        self._check_p015(node)
        self._check_p016(node)
        self._check_p017_raise(node)
        self._check_p018(node)
        self._in_raise_call = True
        self.generic_visit(node)
        self._in_raise_call = False

    def visit_If(self, node: ast.If) -> None:
        self._check_e020(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check_p003(node)
        self._check_p017_call(node)
        self._check_e019(node)
        self.generic_visit(node)
