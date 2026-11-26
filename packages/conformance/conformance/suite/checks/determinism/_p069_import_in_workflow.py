"""P069 — ImportInWorkflow.

Flags an ``import`` / ``from … import`` statement that runs while a workflow
executes: one inside a workflow-context method of an ``App`` subclass, or one
inside a function of this repo that such a method calls.

Temporal builds a workflow instance on the worker's event-loop thread, where no
deadlock timer runs, so the imports at the top of a module cost nothing at run
time.  Every later activation runs on a workflow thread that Temporal interrupts
after 2 seconds (``TMPRL1101``).  An import inside the method body runs there, and
the sandbox re-executes any module it has not loaded yet — the app's own package
is never passed through.  On a cold, busy worker that import overruns the timer,
and the interrupt can land while the thread waits on that module's import lock,
leaving the lock held.  Every later import of that module, and of the module that
was importing it, hangs.  Every workflow run imports the same modules, so the whole
worker stops making progress while its pod stays up.  The global import lock is
not involved: imports of unrelated modules still work.

Imports guarded by ``if TYPE_CHECKING:`` never run and are not flagged; an
``else`` branch of that guard does run and is scanned.

Reach into helpers
------------------
Workflow logic often lives in helpers: an ``@entrypoint`` that delegates to a
module-level ``run_*_flow()`` in another file.  The rule follows calls from a
workflow-context method into functions this repo defines, transitively:

* a top-level function of the same module, called by name;
* a function imported from a module under the scanned root
  (``from app.flow import run_flow`` / ``from .flow import run_flow``), called
  by name;
* a function of a repo module imported as a module (``from app import flow`` /
  ``import app.flow as flow``), called as ``flow.run_flow()``;
* a method of the same class, called through ``self``.

Sync and async helpers are both followed — an awaited helper runs on the same
workflow thread.  A ``@task`` method is never followed: calling it from
workflow code schedules an activity, whose body runs outside the sandbox.
Calls on anything else (a third-party module, an inherited method, a local
variable) are not resolved.  A name bound in the calling function (a parameter,
an assignment, a local import or ``def``) shadows the module-level function or
import of the same name, so a call through it is not followed either.

Imports and calls under ``if TYPE_CHECKING:`` are skipped, but its ``else`` /
``elif`` branch runs and is scanned.

A reached import is reported at the call site in the workflow method, naming
the import and the chain that reaches it, so the reader sees a line in the
file they are looking at.  One finding per call site: the first unsuppressed
import the call reaches.  A ``# conformance: ignore[P069]`` on the import inside
the helper counts, so a reviewed import is waived once for every caller.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    make_finding,
    repo_module_file,
    safe_read_text,
)
from conformance.suite.checks.prescriptions._decorator_provenance import (
    ImportProvenance,
    collect_import_provenance,
    is_task_decorator,
)
from conformance.suite.schema.findings import Finding

from ._workflow_methods import workflow_method_nodes

RULE_ID = "P069"

_HINT = (
    "Move it to module level: inside workflow code it runs on the workflow thread "
    "under Temporal's 2s deadlock timer, and an interrupt mid-import can leave "
    "that module's import lock held, hanging every later import of it and "
    "stalling the worker."
)

_Function = ast.FunctionDef | ast.AsyncFunctionDef
_Import = ast.Import | ast.ImportFrom


def _is_type_checking_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _children(node: ast.AST) -> list[ast.AST]:
    """*node*'s children, with each ``if TYPE_CHECKING:`` replaced by its ``else``.

    The guarded body never runs, but ``TYPE_CHECKING`` is false at run time, so
    the ``else`` (or ``elif``) branch does.
    """
    children: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.If) and _is_type_checking_guard(child):
            children.extend(child.orelse)
        else:
            children.append(child)
    return children


def _walk(node: ast.AST) -> list[ast.AST]:
    """Every node under *node* that runs, skipping ``if TYPE_CHECKING:`` bodies."""
    found: list[ast.AST] = []
    for child in _children(node):
        found.append(child)
        found.extend(_walk(child))
    return found


def _imports(node: ast.AST) -> list[_Import]:
    """Every import under *node* that runs, skipping ``if TYPE_CHECKING:`` bodies."""
    return [n for n in _walk(node) if isinstance(n, (ast.Import, ast.ImportFrom))]


_Scope = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def _local_names(scope: _Scope) -> frozenset[str]:
    """Names *scope* binds locally, which shadow a module-level name of the same
    spelling for the whole function: parameters, assignment targets, local
    imports, nested ``def`` / ``class`` names, ``except … as`` and ``match``
    captures.  ``global`` / ``nonlocal`` names are left out; bodies of nested
    functions are not entered (their bindings are their own).
    """
    args = scope.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    names = {arg.arg for arg in params}
    names.update(arg.arg for arg in (args.vararg, args.kwarg) if arg is not None)
    declared: set[str] = set()
    stack: list[ast.AST] = (
        list(scope.body) if isinstance(scope.body, list) else [scope.body]
    )
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(
                (alias.asname or alias.name).partition(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        stack.extend(ast.iter_child_nodes(node))
    return frozenset(names - declared)


def _calls(scope: _Scope) -> list[tuple[ast.Call, frozenset[str]]]:
    """Every call under *scope* that runs, with the local names it sees.

    A call in a nested function sees that function's locals as well as every
    enclosing function's, which is how Python resolves the name.
    """
    found: list[tuple[ast.Call, frozenset[str]]] = []

    def visit(node: ast.AST, shadowed: frozenset[str]) -> None:
        for child in _children(node):
            inner = shadowed
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                inner = shadowed | _local_names(child)
            elif isinstance(child, ast.Call):
                found.append((child, shadowed))
            visit(child, inner)

    visit(scope, _local_names(scope))
    return found


def _module_name(node: _Import) -> str:
    if isinstance(node, ast.ImportFrom):
        return "." * node.level + (node.module or "")
    return ", ".join(alias.name for alias in node.names)


def _is_suppressed(line: int, directives: dict[int, _IgnoreDirective]) -> bool:
    """The same line-or-comment-above test ``make_finding`` applies."""
    for check_line in (line, line - 1):
        directive = directives.get(check_line)
        if directive is None or (check_line == line - 1 and not directive.comment_only):
            continue
        if directive.rule_ids is None or RULE_ID in directive.rule_ids:
            return True
    return False


@dataclass
class _Module:
    """One parsed source file the walk can resolve calls in."""

    filename: str
    tree: ast.Module
    directives: dict[int, _IgnoreDirective]
    path: Path | None

    @cached_property
    def provenance(self) -> ImportProvenance:
        return collect_import_provenance(self.tree)

    @cached_property
    def functions(self) -> dict[str, _Function]:
        return {
            node.name: node
            for node in self.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    @cached_property
    def from_imports(self) -> dict[str, tuple[ast.ImportFrom, str]]:
        """Local name → (the ``from … import`` that binds it, the imported name)."""
        return {
            alias.asname or alias.name: (node, alias.name)
            for node in self.tree.body
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }

    @cached_property
    def module_aliases(self) -> dict[str, str]:
        """Local name → dotted module, for ``import a.b as c`` (``c``)."""
        return {
            alias.asname: alias.name
            for node in self.tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.asname is not None
        }

    def methods(self, cls: ast.ClassDef) -> dict[str, _Function]:
        return {
            node.name: node
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }


@dataclass(frozen=True)
class _Reached:
    """The first unsuppressed import a call reaches, and the chain to it."""

    module: str
    filename: str
    line: int
    chain: tuple[str, ...]


@dataclass(frozen=True)
class _Target:
    module: _Module
    function: _Function
    cls: ast.ClassDef | None


class _Reach:
    """Transitive call-graph walk from workflow code into this repo's helpers."""

    def __init__(self, module: _Module, root: Path | None) -> None:
        self._root = root
        self._loaded: dict[Path, _Module | None] = {}
        if module.path is not None:
            self._loaded[module.path.resolve()] = module
        # Memo per function: the import it reaches, if any. A function under
        # walk is marked None first so a call cycle terminates.
        self._memo: dict[int, _Reached | None] = {}

    def _load(self, source: Path) -> _Module | None:
        source = source.resolve()
        if source not in self._loaded:
            module: _Module | None = None
            text = safe_read_text(source)
            if text is not None:
                try:
                    tree = ast.parse(text, filename=str(source))
                except SyntaxError:
                    tree = None
                if tree is not None:
                    root = self._root.resolve() if self._root is not None else None
                    rel = source.relative_to(root) if root is not None else source
                    module = _Module(
                        rel.as_posix(), tree, _parse_directives(text), source
                    )
            self._loaded[source] = module
        return self._loaded[source]

    def _repo_module(
        self, module: _Module, dotted: str | None, level: int
    ) -> _Module | None:
        source = repo_module_file(self._root, dotted, level, module.path)
        return self._load(source) if source is not None else None

    def _from_import(self, module: _Module, name: str) -> _Target | None:
        bound = module.from_imports.get(name)
        if bound is None:
            return None
        node, original = bound
        imported = self._repo_module(module, node.module, node.level)
        if imported is None:
            return None
        function = imported.functions.get(original)
        return _Target(imported, function, None) if function is not None else None

    def _module_ref(self, module: _Module, name: str) -> _Module | None:
        """The repo module a local *name* is bound to, if it is one."""
        dotted = module.module_aliases.get(name)
        if dotted is not None:
            return self._repo_module(module, dotted, 0)
        bound = module.from_imports.get(name)
        if bound is None:
            return None
        node, original = bound
        package = ".".join(p for p in (node.module, original) if p)
        return self._repo_module(module, package, node.level)

    def resolve(
        self,
        module: _Module,
        call: ast.Call,
        cls: ast.ClassDef | None,
        shadowed: frozenset[str],
    ) -> _Target | None:
        """The repo function *call* runs, if it can be named statically.

        *shadowed* is the set of names bound in the calling function: a call
        through one of them runs whatever was bound locally, not the module's
        function or import of the same name, so it is not followed.
        """
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in shadowed:
                return None
            local = module.functions.get(func.id)
            if local is not None:
                return _Target(module, local, None)
            return self._from_import(module, func.id)
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return None
        receiver = func.value.id
        if receiver == "self" and cls is not None:
            method = module.methods(cls).get(func.attr)
            if method is None or any(
                is_task_decorator(dec, module.provenance)
                for dec in method.decorator_list
            ):
                return None
            return _Target(module, method, cls)
        if receiver in shadowed:
            return None
        target_module = self._module_ref(module, receiver)
        if target_module is None:
            return None
        function = target_module.functions.get(func.attr)
        return _Target(target_module, function, None) if function is not None else None

    def reach(self, target: _Target) -> _Reached | None:
        """The first unsuppressed import *target* runs, directly or via a callee."""
        key = id(target.function)
        if key in self._memo:
            return self._memo[key]
        self._memo[key] = None
        label = target.function.name
        result: _Reached | None = None
        for node in _imports(target.function):
            if not _is_suppressed(node.lineno, target.module.directives):
                result = _Reached(
                    _module_name(node), target.module.filename, node.lineno, (label,)
                )
                break
        if result is None:
            for call, shadowed in _calls(target.function):
                callee = self.resolve(target.module, call, target.cls, shadowed)
                found = self.reach(callee) if callee is not None else None
                if found is not None:
                    result = _Reached(
                        found.module, found.filename, found.line, (label, *found.chain)
                    )
                    break
        self._memo[key] = result
        return result


def _enclosing_classes(tree: ast.AST) -> dict[int, ast.ClassDef]:
    return {
        id(member): node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for member in node.body
    }


def check_p069(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
    *,
    path: Path | None = None,
    root: Path | None = None,
) -> list[Finding]:
    """Emit P069 findings for imports that run inside workflow-context code.

    *path* and *root* let the walk follow a call into a function imported from
    the scanned repo.  Without them, only this file's functions are followed.
    """
    methods = workflow_method_nodes(tree)
    if not methods or not isinstance(tree, ast.Module):
        return []
    module = _Module(filename, tree, directives, path)
    reach = _Reach(module, root)
    owners = _enclosing_classes(tree)
    workflow_ids = {id(method) for method in methods}
    findings: list[Finding] = []
    for method in methods:
        findings.extend(
            make_finding(
                filename=filename,
                rule_id=RULE_ID,
                node=node,
                message=f"Import of '{_module_name(node)}' in workflow context. {_HINT}",
                directives=directives,
            )
            for node in _imports(method)
        )
        cls = owners.get(id(method))
        for call, shadowed in _calls(method):
            target = reach.resolve(module, call, cls, shadowed)
            # Another workflow-context method is scanned on its own; following
            # it here would report its imports twice.
            if target is None or id(target.function) in workflow_ids:
                continue
            reached = reach.reach(target)
            if reached is None:
                continue
            chain = " -> ".join(f"{name}()" for name in reached.chain)
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id=RULE_ID,
                    node=call,
                    message=(
                        f"Call to {chain} reaches an import of '{reached.module}' "
                        f"at {reached.filename}:{reached.line}, which runs in "
                        f"workflow context. {_HINT}"
                    ),
                    directives=directives,
                )
            )
    return findings
