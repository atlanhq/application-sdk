"""Shared AST plumbing for the worker-disk rules (P060-P062, O010).

Those rules ask three questions about one function body:

* **Which calls are the SDK's storage helpers or the stdlib ``tempfile``
  functions?**  Resolved through the module's import bindings
  (:class:`StorageBindings`, :class:`TempfileBindings`), the approach O001 takes
  for stdlib ``json``, so an app helper or a client method that shares a name is
  never matched.
* **Which names does a local path flow into?**  :class:`ScopeFlow` follows
  path-preserving expressions only — aliases, ``Path(...)``, ``os.path.join``,
  ``/``, ``+`` and f-string construction, directory listings, ``FileReference``
  constructors and container mutators — not arbitrary calls, so a row count
  read from a file is not mistaken for the file.
* **Where is that path removed, and does it leave the function?**  A removal is
  ``os.remove``/``os.unlink``/``os.rmdir``/``shutil.rmtree``, a ``.unlink()``/
  ``.rmdir()`` method, a helper whose name says it removes, or any call handed a
  removal callable (``asyncio.to_thread(shutil.rmtree, d)``,
  ``stack.callback(shutil.rmtree, d)``).  Each removal records whether it sits in
  a ``finally`` (a registered callback counts) and which loops enclose it.  A
  path *escapes* when it, or a path built from it, is returned, yielded, stored
  on an attribute or subscript, or declared ``global``/``nonlocal``: whoever
  receives it owns it.

Matching is lenient in the direction that silences a finding — a removal of an
ancestor covers a file under it, a name the path was built from counts — because
the BLOCK-tier rules built on this must not fire on code that already cleans up.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ._ast_common import collect_import_origins

__all__ = [
    "DEV_TOOL_DIRS",
    "LOCAL_ARG",
    "SDK_STORAGE_MODULES",
    "CallSite",
    "ModuleContext",
    "Removal",
    "ScopeFlow",
    "StorageBindings",
    "TempfileBindings",
    "call_arg",
    "has_kwargs_splat",
    "iter_scopes",
    "runs_on_worker",
    "same_store",
]

#: SDK modules from which each storage helper resolves to the SDK function (or a
#: deprecated alias with the same signature and defaults).  Every module that
#: binds the name at top level counts, accidental re-exports included;
#: ``tests/test_local_files.py`` re-derives the lists from the installed SDK.
SDK_STORAGE_MODULES: dict[str, frozenset[str]] = {
    "upload_file": frozenset(
        {
            "application_sdk.storage",
            "application_sdk.storage.ops",
            "application_sdk.storage.batch",
            "application_sdk.storage.cloud",
            "application_sdk.common.incremental.marker",
            "application_sdk.common.incremental.state.store",
            "application_sdk.testing.harness.seed",
        }
    ),
    "upload_prefix": frozenset(
        {
            "application_sdk.storage",
            "application_sdk.storage.batch",
            "application_sdk.common.incremental.state.state_writer",
            "application_sdk.common.incremental.state.store",
        }
    ),
    "download_file": frozenset(
        {
            "application_sdk.storage",
            "application_sdk.storage.ops",
            "application_sdk.common.incremental.helpers",
        }
    ),
    "download_prefix": frozenset(
        {
            "application_sdk.storage",
            "application_sdk.storage.batch",
            "application_sdk.common.incremental.helpers",
            "application_sdk.common.incremental.state.state_reader",
            "application_sdk.common.incremental.state.state_writer",
        }
    ),
}

#: ``(position, keyword)`` of each helper's local-path parameter.
LOCAL_ARG: dict[str, tuple[int, str]] = {
    "upload_file": (1, "local_path"),
    "download_file": (1, "local_path"),
    "upload_prefix": (0, "local_dir"),
    "download_prefix": (1, "local_dir"),
}

#: ``(position, keyword)`` of the ``store`` parameter, the same on all four.
_STORE_ARG = (2, "store")

_SDK_ROOT = "application_sdk"

_TEMPFILE = "tempfile"

#: Removal callables, by qualified import origin.
_REMOVAL_FUNCS = frozenset(
    {"os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree"}
)

#: Methods that remove their receiver (``pathlib.Path``).
_REMOVAL_METHODS = frozenset({"unlink", "rmdir"})

#: A helper whose name says it removes local files (``delete_local_files``,
#: ``safe_delete``, ``cleanup``).  The verb must stand as its own word, so
#: ``deleted_rows`` is not one.  Lenient on purpose: treating a helper as a
#: removal can only silence a finding.
_REMOVALISH = re.compile(
    r"(?:^|_)(?:remove|unlink|rmtree|rmdir|delete|cleanup|clean_up|purge|discard"
    r"|wipe|erase)(?:_|$)",
    re.IGNORECASE,
)

#: Calls that register a callback to run later, on every exit path.
_REGISTRATION_NAMES = frozenset(
    {"callback", "push", "register", "finalize", "addCleanup", "add_done_callback"}
)

#: Calls that run their first (``run_in_executor``: second) argument with the
#: rest, so that argument is the real callee.
_OFFLOAD_NAMES = {"to_thread": 0, "run_in_thread": 0, "run_in_executor": 1}

#: Callables whose result is the same path, or paths under it.
_PATH_FUNCS = frozenset(
    {
        "Path",
        "PurePath",
        "PosixPath",
        "PurePosixPath",
        "str",
        "fspath",
        "join",
        "joinpath",
        "abspath",
        "realpath",
        "normpath",
        "expanduser",
        "dirname",
        "glob",
        "iglob",
        "rglob",
        "listdir",
        "scandir",
        "walk",
        "iterdir",
        "sorted",
        "list",
        "tuple",
        "set",
        "enumerate",
        "reversed",
        "from_local",
        "FileReference",
        "with_suffix",
        "with_name",
        "with_stem",
        "resolve",
        "absolute",
        "as_posix",
        "relative_to",
    }
)

#: Methods that put their argument into the receiver.
_MUTATORS = frozenset(
    {
        "append",
        "extend",
        "add",
        "insert",
        "update",
        "setdefault",
        "appendleft",
        "put",
        "put_nowait",
    }
)

#: Bare names that never denote a path.
_NOT_PATHS = frozenset({"self", "cls"})

#: An app helper whose name says it produces a local path from its arguments
#: (``_download_seeds(input, retry_dir)``, ``build_output_path(base)``,
#: ``scratch_dir_for(run)``).  Lenient on purpose: treating a result as a path
#: under its arguments can only make a removal or an escape match.
_PATHISH = re.compile(
    r"^_*(?:download|fetch|stage|materiali[sz]e|copy|mkdir|make_dirs?|ensure_dir"
    r"|prepare)"
    r"|(?:^|_)(?:paths?|dirs?|directory|filepath|scratch|workdir|folder)(?:_|$)",
    re.IGNORECASE,
)

#: Storage helpers whose results are store keys or digests, not local paths.
_NOT_PATH_RESULTS = frozenset(
    {"upload_file", "upload_prefix", "download_file", "upload_file_from_bytes"}
)


def _returns_path(name: str | None) -> bool:
    """Whether a call to *name* returns a path built from its arguments."""
    if name is None or name in _NOT_PATH_RESULTS:
        return False
    return name in _PATH_FUNCS or _PATHISH.search(name) is not None


#: ``tempfile`` context managers that delete their path on exit (unless
#: ``delete=False``).
_SELF_CLEANING = frozenset(
    {
        "TemporaryDirectory",
        "NamedTemporaryFile",
        "TemporaryFile",
        "SpooledTemporaryFile",
    }
)

#: Exception names an ``except`` must catch to see every failure.
_CATCH_ALL = frozenset({"Exception", "BaseException"})


def _catches_everything(handler: ast.ExceptHandler) -> bool:
    """Bare ``except:``, ``except Exception`` or ``except BaseException``."""
    kind = handler.type
    if kind is None:
        return True
    names = kind.elts if isinstance(kind, ast.Tuple) else [kind]
    return any(_last(name) in _CATCH_ALL for name in names)


# ---------------------------------------------------------------------------
# Import bindings
# ---------------------------------------------------------------------------


def _dotted(expr: ast.expr) -> str | None:
    """``a.b.c`` for a pure Name/Attribute chain, else ``None``."""
    parts: list[str] = []
    node: ast.expr = expr
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


#: Single-entry cache of :func:`_imports` (the module being scanned).  Holding
#: the tree keeps its id from being reused while it is cached.
_IMPORTS_CACHE: list[tuple[ast.AST, list[ast.Import | ast.ImportFrom]]] = []


def _imports(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """Every import statement in *tree*, walked once per module."""
    if _IMPORTS_CACHE and _IMPORTS_CACHE[0][0] is tree:
        return _IMPORTS_CACHE[0][1]
    found = [
        node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    _IMPORTS_CACHE[:] = [(tree, found)]
    return found


def _last(expr: ast.expr) -> str | None:
    """The final segment of a Name/Attribute callee (``os.path.join`` → ``join``)."""
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    return None


class StorageBindings:
    """The SDK storage helpers a module can reach, by local name.

    ``from M import upload_file [as x]`` binds a helper; ``import M as x`` and
    ``from <package> import <module>`` bind a module whose ``.upload_file(...)``
    is the helper; a plain ``import application_sdk...`` lets the fully dotted
    path resolve.  Relative imports are skipped: an app never reaches the SDK
    through one.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.functions: dict[str, str] = {}
        self.modules: dict[str, str] = {}
        self.sdk_root_bound = False
        every_module = frozenset().union(*SDK_STORAGE_MODULES.values())
        for node in _imports(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level != 0 or node.module is None:
                    continue
                for alias in node.names:
                    bound = alias.asname or alias.name
                    modules = SDK_STORAGE_MODULES.get(alias.name)
                    if modules is not None and node.module in modules:
                        self.functions[bound] = alias.name
                    elif f"{node.module}.{alias.name}" in every_module:
                        self.modules[bound] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname is not None:
                        if alias.name in every_module:
                            self.modules[alias.asname] = alias.name
                    elif alias.name == _SDK_ROOT or alias.name.startswith(
                        f"{_SDK_ROOT}."
                    ):
                        self.sdk_root_bound = True

    @property
    def empty(self) -> bool:
        """True when the module binds none of the helpers."""
        return not self.functions and not self.modules and not self.sdk_root_bound

    def resolve(self, call: ast.Call) -> str | None:
        """The SDK helper *call* invokes (``"upload_file"``, …), else ``None``."""
        func = call.func
        if isinstance(func, ast.Name):
            return self.functions.get(func.id)
        if not isinstance(func, ast.Attribute):
            return None
        modules = SDK_STORAGE_MODULES.get(func.attr)
        if modules is None:
            return None
        if isinstance(func.value, ast.Name) and func.value.id in self.modules:
            return func.attr if self.modules[func.value.id] in modules else None
        if self.sdk_root_bound and _dotted(func.value) in modules:
            return func.attr
        return None


class TempfileBindings:
    """Local names bound to the stdlib ``tempfile`` module and its functions."""

    def __init__(self, tree: ast.AST) -> None:
        modules: set[str] = set()
        self.functions: dict[str, str] = {}
        for node in _imports(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == _TEMPFILE:
                        modules.add(alias.asname or _TEMPFILE)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module == _TEMPFILE:
                    for alias in node.names:
                        self.functions[alias.asname or alias.name] = alias.name
        self.modules = frozenset(modules)

    @property
    def empty(self) -> bool:
        """True when the module binds nothing from ``tempfile``."""
        return not self.modules and not self.functions

    def resolve(self, func: ast.expr) -> str | None:
        """The stdlib ``tempfile`` function *func* names, else ``None``."""
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in self.modules
        ):
            return func.attr
        if isinstance(func, ast.Name):
            return self.functions.get(func.id)
        return None


#: Top-level directories of an app repo that hold developer tooling — one-off
#: scripts, benchmarks, proofs of concept — rather than code a worker runs.
DEV_TOOL_DIRS = frozenset(
    {"scripts", "tools", "poc", "bench", "benchmarks", "examples", "notebooks"}
)


def runs_on_worker(filename: str) -> bool:
    """False for a file under a top-level developer-tooling directory: the
    worker-disk rules grade the disk of a worker, which never runs that code."""
    parts = PurePosixPath(filename.replace("\\", "/")).parts
    return not (len(parts) > 1 and parts[0] in DEV_TOOL_DIRS)


def call_arg(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    """The argument bound to *keyword* (or passed at *position*), if knowable."""
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    if any(isinstance(arg, ast.Starred) for arg in call.args[: position + 1]):
        return None
    return call.args[position] if len(call.args) > position else None


def has_kwargs_splat(call: ast.Call) -> bool:
    """True when a ``**kwargs`` splat may carry any keyword."""
    return any(kw.arg is None for kw in call.keywords)


def same_store(first: ast.Call, second: ast.Call) -> bool:
    """Whether two storage calls provably target the same store.

    Both omitting ``store`` (the infrastructure default) or both passing the same
    expression counts; anything else may be a cross-store move.
    """
    a = call_arg(first, *_STORE_ARG)
    b = call_arg(second, *_STORE_ARG)
    if a is None or b is None:
        return a is None and b is None
    return ast.dump(a) == ast.dump(b)


# ---------------------------------------------------------------------------
# Function-scope flow
# ---------------------------------------------------------------------------


class ModuleContext:
    """Per-module facts every scope of the module shares, computed once.

    Holds the import bindings, the module-local *removers* (functions that
    remove a path they are given, so ``_cleanup(path)`` counts as removing
    ``path``) and a memo of each scope's :class:`ScopeFlow`.
    """

    def __init__(self, tree: ast.AST) -> None:
        modules: set[str] = set()
        functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.asname or alias.name.split(".", 1)[0])
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                functions.append(node)
        self.origins: dict[str, str] = collect_import_origins(tree)
        """Each import-bound name → its qualified origin."""
        self.module_names = frozenset(modules)
        """Names bound by ``import X [as Y]``: modules, never paths."""
        self.tempfile = TempfileBindings(tree)
        """The module's stdlib ``tempfile`` bindings."""
        self._functions = functions
        self._removers: dict[str, tuple[frozenset[int], frozenset[str]]] | None = None
        self._flows: dict[int, ScopeFlow] = {}

    @classmethod
    def of(cls, tree: ast.AST) -> ModuleContext:
        """The context for *tree*, cached for the module last asked about —
        every worker-disk check of one file shares it."""
        if _CONTEXT_CACHE and _CONTEXT_CACHE[0][0] is tree:
            return _CONTEXT_CACHE[0][1]
        context = cls(tree)
        _CONTEXT_CACHE[:] = [(tree, context)]
        return context

    @property
    def removers(self) -> dict[str, tuple[frozenset[int], frozenset[str]]]:
        """Module-local function name → (positions, names) of the parameters it
        removes (``self``/``cls`` not counted)."""
        if self._removers is None:
            self._removers = {}  # what a summary sees while it is being built
            # A helper that removes by calling another helper is one more hop;
            # a few rounds cover the shapes apps write, stopping once stable.
            for _ in range(3):
                known = frozenset(self._removers)
                candidates = [f for f in self._functions if _may_remove(f, known)]
                updated = _removers(candidates, self)
                if updated == self._removers:
                    break
                self._removers = updated
        return self._removers

    def flow(self, scope: ast.AST) -> ScopeFlow:
        """The (memoized) :class:`ScopeFlow` for *scope*."""
        key = id(scope)
        if key not in self._flows:
            self.removers  # noqa: B018 — settle the summaries before any memoized flow
            self._flows[key] = ScopeFlow.build(scope, self)
        return self._flows[key]


#: Single-entry cache for :meth:`ModuleContext.of` (the module being scanned).
#: Holding the tree keeps its id from being reused while it is cached.
_CONTEXT_CACHE: list[tuple[ast.AST, ModuleContext]] = []


def _may_remove(
    function: ast.FunctionDef | ast.AsyncFunctionDef, known: frozenset[str]
) -> bool:
    """Cheap syntactic pre-check: does *function* call anything that removes?"""
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        name = _last(node.func)
        if name is None:
            continue
        if (
            name in _REMOVAL_METHODS
            or name in {"remove", "rmtree", "removedirs"}
            or name in known
            or _REMOVALISH.search(name) is not None
            or any(_last(arg) in {"remove", "unlink", "rmtree"} for arg in node.args)
        ):
            return True
    return False


def _removers(
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef], context: ModuleContext
) -> dict[str, tuple[frozenset[int], frozenset[str]]]:
    """Which parameters each candidate function or method removes."""
    out: dict[str, tuple[frozenset[int], frozenset[str]]] = {}
    for function in functions:
        flow = ScopeFlow.build(function, context)
        if not flow.removals:
            continue
        positional = [a.arg for a in (*function.args.posonlyargs, *function.args.args)]
        if positional and positional[0] in _NOT_PATHS:
            positional = positional[1:]
        candidates = [*positional, *(a.arg for a in function.args.kwonlyargs)]
        removed = frozenset(
            name
            for name in candidates
            if any(flow.related(frozenset({name}), r.targets) for r in flow.removals)
        )
        if not removed:
            continue
        positions = frozenset(i for i, name in enumerate(positional) if name in removed)
        if function.name in out:
            prev_positions, prev_names = out[function.name]
            positions, removed = positions | prev_positions, removed | prev_names
        out[function.name] = (positions, removed)
    return out


@dataclass(frozen=True)
class Removal:
    """One call (or context-manager exit) that removes local paths."""

    node: ast.AST
    """The removing call, or the ``with`` whose exit removes a temp path."""
    targets: frozenset[str]
    """Path tokens of what it removes."""
    in_finally: bool
    """Runs on every exit: inside a ``finally`` body, a registered cleanup
    callback, or a ``tempfile`` context manager's exit."""
    loops: tuple[ast.AST, ...]
    """Enclosing loops within the scope, outermost first."""
    in_handler: bool = False
    """Inside an ``except`` handler that catches everything (bare,
    ``Exception`` or ``BaseException``)."""

    @property
    def position(self) -> tuple[int, int]:
        """Where the removal happens: a ``with`` removes at its end."""
        if isinstance(self.node, ast.With | ast.AsyncWith):
            return (self.node.end_lineno or 0, self.node.end_col_offset or 0)
        return _position(self.node)

    @property
    def line(self) -> int:
        return self.position[0]


@dataclass(frozen=True)
class CallSite:
    """One call in the scope, with its lexical context."""

    node: ast.Call
    in_finally: bool
    loops: tuple[ast.AST, ...]


@dataclass
class ScopeFlow:
    """Path flow, escapes and removals for one function (or module) body.

    Nested function and class bodies are separate scopes and are not entered;
    lambda bodies are, because a cleanup callback is usually one.
    """

    scope: ast.AST
    context: ModuleContext
    deps: dict[str, set[str]] = field(default_factory=dict)
    escaping: set[str] = field(default_factory=set)
    removals: list[Removal] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    bindings: list[tuple[ast.expr, ast.expr]] = field(default_factory=list)
    """``(target, value)`` of every assignment, in source order."""
    loads: list[tuple[str, int, int]] = field(default_factory=list)

    @classmethod
    def build(cls, scope: ast.AST, context: ModuleContext) -> ScopeFlow:
        flow = cls(scope=scope, context=context)
        _Walker(flow).run()
        return flow

    # -- tokens ---------------------------------------------------------------

    def chain(self, expr: ast.expr) -> str | None:
        """The path token for a Name/Attribute chain, unless it names a module."""
        dotted = _dotted(expr)
        if dotted is None or dotted in _NOT_PATHS:
            return None
        if dotted.split(".", 1)[0] in self.context.module_names:
            return None
        return dotted

    def tokens(self, expr: ast.expr | None) -> frozenset[str]:
        """Names whose value flows into *expr* as a path."""
        if expr is None:
            return frozenset()
        out: set[str] = set()
        self._collect(expr, out)
        return frozenset(out)

    def _collect(self, expr: ast.expr, out: set[str]) -> None:
        token = self.chain(expr)
        if token is not None:
            out.add(token)
        elif isinstance(expr, ast.Call):
            self._collect_call(expr, out)
        elif isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div | ast.Add):
            self._collect(expr.left, out)
            self._collect(expr.right, out)
        elif isinstance(expr, ast.JoinedStr):
            for value in expr.values:
                if isinstance(value, ast.FormattedValue):
                    self._collect(value.value, out)
        elif isinstance(expr, ast.Attribute | ast.Subscript | ast.Starred | ast.Await):
            self._collect(expr.value, out)
        elif isinstance(expr, ast.List | ast.Tuple | ast.Set):
            for element in expr.elts:
                self._collect(element, out)
        elif isinstance(expr, ast.Dict):
            for value in expr.values:
                self._collect(value, out)
        elif isinstance(expr, ast.IfExp):
            self._collect(expr.body, out)
            self._collect(expr.orelse, out)
        elif isinstance(expr, ast.ListComp | ast.SetComp | ast.GeneratorExp):
            self._collect(expr.elt, out)
            for generator in expr.generators:
                self._collect(generator.iter, out)

    def _collect_call(self, call: ast.Call, out: set[str]) -> None:
        func = call.func
        name = _last(func)
        if name in _OFFLOAD_NAMES:
            index = _OFFLOAD_NAMES[name]
            if len(call.args) > index and _returns_path(_last(call.args[index])):
                for arg in call.args[index + 1 :]:
                    self._collect(arg, out)
            return
        if not _returns_path(name):
            return  # data computed from a path is not the path
        if isinstance(func, ast.Attribute):
            self._collect(func.value, out)
        for arg in [*call.args, *(kw.value for kw in call.keywords)]:
            self._collect(arg, out)

    # -- flow -----------------------------------------------------------------

    def upstream(self, tokens: frozenset[str] | set[str]) -> set[str]:
        """*tokens* plus every name they were built from."""
        return _closure(tokens, self.deps)

    def downstream(self, tokens: frozenset[str] | set[str]) -> set[str]:
        """*tokens* plus every name built from them."""
        reverse: dict[str, set[str]] = {}
        for target, sources in self.deps.items():
            for source in sources:
                reverse.setdefault(source, set()).add(target)
        return _closure(tokens, reverse)

    def related(self, a: frozenset[str], b: frozenset[str]) -> bool:
        """Whether two token sets name the same tree: one built from the other,
        or one an attribute of the other (``f.name`` and ``f``)."""
        up_a = _with_prefixes(self.upstream(a))
        up_b = _with_prefixes(self.upstream(b))
        return bool(_with_prefixes(a) & up_b) or bool(_with_prefixes(b) & up_a)

    def parameter_source(self, tokens: frozenset[str]) -> str | None:
        """The function parameter the path was built from, if any — the caller
        then decides how long the file has to live."""
        scope = self.scope
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            return None
        args = scope.args
        params = [
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            *(arg for arg in (args.vararg, args.kwarg) if arg is not None),
        ]
        names = self.upstream(tokens)
        return next((p.arg for p in params if p.arg in names), None)

    def escapes(self, tokens: frozenset[str]) -> bool:
        """Whether the path, a path built from it, or a directory it sits under
        leaves this scope — a returned ``FileReference`` to the directory covers
        every file in it."""
        return bool((self.downstream(tokens) | self.upstream(tokens)) & self.escaping)

    def removal_of(
        self,
        tokens: frozenset[str],
        *,
        after: ast.AST | None = None,
        within: ast.AST | None = None,
    ) -> Removal | None:
        """A removal of *tokens* (or of the tree holding them), preferring one in
        a ``finally``.

        *after* keeps only removals positioned after that node; *within* keeps
        only removals inside that loop's body.
        """
        best: Removal | None = None
        for removal in self._removals_of(tokens, after=after, within=within):
            if best is None or (removal.in_finally and not best.in_finally):
                best = removal
        return best

    def removed_on_every_exit(self, tokens: frozenset[str], *, after: ast.AST) -> bool:
        """Whether *tokens* are removed on every exit path after *after*.

        A removal in a ``finally`` (or a registered callback, or a ``tempfile``
        context manager's exit) does it alone; so does the pair of a removal in
        an ``except`` that catches everything and one on the success path.
        """
        found = list(self._removals_of(tokens, after=after))
        if any(removal.in_finally for removal in found):
            return True
        return any(r.in_handler for r in found) and any(not r.in_handler for r in found)

    def _removals_of(
        self,
        tokens: frozenset[str],
        *,
        after: ast.AST | None = None,
        within: ast.AST | None = None,
    ) -> Iterator[Removal]:
        floor = _position(after) if after is not None else None
        for removal in self.removals:
            if floor is not None and removal.position <= floor:
                continue
            if within is not None and within not in removal.loops:
                continue
            if self.related(tokens, removal.targets):
                yield removal

    def last_read(self, tokens: frozenset[str], after: ast.Call) -> int | None:
        """Line of the last read of the path (or a path built from it) after *after*."""
        names = self.downstream(tokens)
        end = (after.end_lineno or after.lineno, after.end_col_offset or 0)
        lines = [
            line
            for token, line, col in self.loads
            if token in names and (line, col) > end
        ]
        return max(lines) if lines else None

    def bound_name(self, call: ast.Call, *, element: int | None = None) -> str | None:
        """The local name *call*'s result is assigned to, through path wrappers.

        ``d = tempfile.mkdtemp()`` and ``d = Path(tempfile.mkdtemp())`` give
        ``d``; with *element*, ``fd, path = tempfile.mkstemp()`` gives the
        tuple element at that index (a plain ``tmp = tempfile.mkstemp()`` gives
        ``tmp``).  Anything else — returned, stored on an attribute, passed
        straight to a call — is ``None``.
        """
        for target, value in self.bindings:
            if _unwrap(value) is not call:
                continue
            if element is not None and isinstance(target, ast.Tuple | ast.List):
                if len(target.elts) > element:
                    return self.chain(target.elts[element])
                return None
            return self.chain(target) if isinstance(target, ast.Name) else None
        return None


def _with_prefixes(tokens: frozenset[str] | set[str]) -> set[str]:
    """*tokens* plus each one's dotted prefixes (``f.name`` → ``f``), never a bare
    ``self``/``cls``, so two attributes of one object do not relate through it."""
    out = set(tokens)
    for token in tokens:
        parts = token.split(".")
        for end in range(1, len(parts)):
            prefix = ".".join(parts[:end])
            if prefix not in _NOT_PATHS:
                out.add(prefix)
    return out


def _closure(start: frozenset[str] | set[str], edges: dict[str, set[str]]) -> set[str]:
    seen = set(start)
    stack = list(start)
    while stack:
        for nxt in edges.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _unwrap(value: ast.expr) -> ast.expr:
    """Strip path wrappers: ``Path(x)``, ``str(x)``, ``Path(x).resolve()`` → ``x``."""
    while isinstance(value, ast.Call):
        name = _last(value.func)
        if name not in _PATH_FUNCS:
            break
        if isinstance(value.func, ast.Attribute) and not value.args:
            value = value.func.value
        elif value.args:
            value = value.args[0]
        else:
            break
    return value


class _Walker(ast.NodeVisitor):
    """One pass over a scope, filling a :class:`ScopeFlow`."""

    def __init__(self, flow: ScopeFlow) -> None:
        self.flow = flow
        self._finally = 0
        self._handler = 0
        self._loops: list[ast.AST] = []

    def run(self) -> None:
        scope = self.flow.scope
        if isinstance(scope, ast.Module):
            for stmt in scope.body:
                if not isinstance(
                    stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                ):
                    self.visit(stmt)
        elif isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            for stmt in scope.body:
                self.visit(stmt)

    # -- scope boundaries -----------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    # -- control flow ---------------------------------------------------------

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        for stmt in node.body:
            self.visit(stmt)
        for handler in node.handlers:
            broad = _catches_everything(handler)
            self._handler += broad
            for stmt in handler.body:
                self.visit(stmt)
            self._handler -= broad
        for stmt in node.orelse:
            self.visit(stmt)
        self._finally += 1
        for stmt in node.finalbody:
            self.visit(stmt)
        self._finally -= 1

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._visit_try(node)

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._bind(node.target, node.iter)
        self._loops.append(node)
        for stmt in node.body:
            self.visit(stmt)
        self._loops.pop()
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_For(self, node: ast.For) -> None:
        self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_for(node)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._loops.append(node)
        for stmt in node.body:
            self.visit(stmt)
        self._loops.pop()
        for stmt in node.orelse:
            self.visit(stmt)

    # -- bindings -------------------------------------------------------------

    def _bind(self, target: ast.expr, value: ast.expr) -> None:
        self.flow.bindings.append((target, value))
        self._bind_tokens(target, self.flow.tokens(value))

    def _bind_tokens(self, target: ast.expr, sources: frozenset[str]) -> None:
        if isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                self._bind_tokens(element, sources)
        elif isinstance(target, ast.Starred):
            self._bind_tokens(target.value, sources)
        elif isinstance(target, ast.Attribute | ast.Subscript):
            # Stored where something outside this scope can reach it.
            self.flow.escaping |= sources
        elif (token := self.flow.chain(target)) is not None and sources:
            self.flow.deps.setdefault(token, set()).update(sources)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self._bind(node.target, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._bind(node.target, node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind(node.target, node.value)

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        managed: list[ast.expr] = []
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind(item.optional_vars, item.context_expr)
                if self._removes_on_exit(item.context_expr):
                    managed.append(item.optional_vars)
        for stmt in node.body:
            self.visit(stmt)
        if managed:
            # ``with tempfile.TemporaryDirectory() as d:`` removes ``d`` when the
            # block exits, however it exits.
            self.flow.removals.append(
                Removal(node, self._targets(managed), True, tuple(self._loops))
            )

    def _removes_on_exit(self, expr: ast.expr) -> bool:
        """A ``tempfile`` context manager that deletes its path on exit."""
        if not isinstance(expr, ast.Call):
            return False
        name = self.flow.context.tempfile.resolve(expr.func)
        if name not in _SELF_CLEANING:
            return False
        delete = next((kw.value for kw in expr.keywords if kw.arg == "delete"), None)
        return not (isinstance(delete, ast.Constant) and delete.value is False)

    def visit_With(self, node: ast.With) -> None:
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_with(node)

    # -- escapes --------------------------------------------------------------

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            self.visit(node.value)
            self.flow.escaping |= self.flow.tokens(node.value)

    def visit_Yield(self, node: ast.Yield) -> None:
        if node.value is not None:
            self.visit(node.value)
            self.flow.escaping |= self.flow.tokens(node.value)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self.visit(node.value)
        self.flow.escaping |= self.flow.tokens(node.value)

    def visit_Global(self, node: ast.Global) -> None:
        self.flow.escaping.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.flow.escaping.update(node.names)

    # -- reads ----------------------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.flow.loads.append((node.id, node.lineno, node.col_offset))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        token = self.flow.chain(node)
        if token is not None and isinstance(node.ctx, ast.Load):
            self.flow.loads.append((token, node.lineno, node.col_offset))
        self.generic_visit(node)

    # -- calls ----------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        loops = tuple(self._loops)
        self.flow.calls.append(CallSite(node, self._finally > 0, loops))
        removal = self._removal(node, loops)
        if removal is not None:
            self.flow.removals.append(removal)
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _MUTATORS
            and (receiver := self.flow.chain(func.value)) is not None
        ):
            sources: set[str] = set()
            for arg in node.args:
                sources |= self.flow.tokens(arg)
            if sources:
                self.flow.deps.setdefault(receiver, set()).update(sources)
        self.generic_visit(node)

    def _qualified(self, expr: ast.expr) -> str | None:
        """``os.remove`` for ``os.remove`` / ``from os import remove`` / an alias."""
        dotted = _dotted(expr)
        if dotted is None:
            return None
        root, _, rest = dotted.partition(".")
        origin = self.flow.context.origins.get(root)
        if origin is None:
            return dotted
        return f"{origin}.{rest}" if rest else origin

    def _is_removal_callable(self, expr: ast.expr) -> bool:
        """A reference to something that removes files: ``shutil.rmtree``,
        ``os.remove``, ``Path.unlink``, or a removal-named helper."""
        qualified = self._qualified(expr)
        if qualified is None:
            return False
        last = qualified.rsplit(".", 1)[-1]
        return (
            qualified in _REMOVAL_FUNCS
            or last in _REMOVAL_METHODS
            or _REMOVALISH.search(last) is not None
        )

    def _removal(self, call: ast.Call, loops: tuple[ast.AST, ...]) -> Removal | None:
        func = call.func
        args = [*call.args, *(kw.value for kw in call.keywords)]
        in_finally = self._finally > 0
        in_handler = self._handler > 0
        handed = [arg for arg in call.args if self._is_removal_callable(arg)]
        if handed:
            # A removal callable handed to another call: an offload
            # (asyncio.to_thread) runs it now, a registration (ExitStack.callback,
            # atexit.register, weakref.finalize) on every exit path.
            targets = self._targets(arg for arg in args if arg not in handed)
            every_exit = in_finally or _last(func) in _REGISTRATION_NAMES
            return Removal(call, targets, every_exit, loops, in_handler)
        if self._qualified(func) in _REMOVAL_FUNCS:
            return Removal(call, self._targets(args), in_finally, loops, in_handler)
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _REMOVAL_METHODS
            and (
                isinstance(func.value, ast.Call)
                or self.flow.chain(func.value) is not None
            )
        ):
            # ``path.unlink()`` / ``Path(p).unlink()``: the receiver is removed.
            targets = self._targets([func.value])
            return Removal(call, targets, in_finally, loops, in_handler)
        if self._is_removal_callable(func):
            return Removal(call, self._targets(args), in_finally, loops, in_handler)
        summary = self._local_remover(func)
        if summary is not None:
            # A helper defined in this module that removes what it is given.
            positions, names = summary
            removed = [
                arg
                for index, arg in enumerate(call.args)
                if index in positions and not isinstance(arg, ast.Starred)
            ]
            removed += [kw.value for kw in call.keywords if kw.arg in names]
            if removed:
                targets = self._targets(removed)
                return Removal(call, targets, in_finally, loops, in_handler)
        return None

    def _local_remover(
        self, func: ast.expr
    ) -> tuple[frozenset[int], frozenset[str]] | None:
        """The removed-parameter summary of a call to a function of this module
        (``helper(...)``, ``self.helper(...)``, ``cls.helper(...)``)."""
        if isinstance(func, ast.Name):
            return self.flow.context.removers.get(func.id)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in _NOT_PATHS
        ):
            return self.flow.context.removers.get(func.attr)
        return None

    def _targets(self, exprs: Iterable[ast.expr]) -> frozenset[str]:
        """Path tokens of a removal's arguments, plus every name chain in them —
        a removal helper's argument shapes are unknown, so be generous."""
        out: set[str] = set()
        for expr in exprs:
            out |= self.flow.tokens(expr)
            callees = {
                id(node.func) for node in ast.walk(expr) if isinstance(node, ast.Call)
            }
            for node in ast.walk(expr):
                if (
                    isinstance(node, ast.Name | ast.Attribute)
                    and id(node) not in callees
                ):
                    token = self.flow.chain(node)
                    if token is not None:
                        out.add(token)
        return frozenset(out)


def iter_scopes(tree: ast.Module) -> Iterator[ast.AST]:
    """The module body and every function body in *tree*, each its own scope."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node
