"""P023 — BlockingCallInAsyncDef.

Enforces "use ``await``, not a sync bridge or blocking lib" inside ``async def``
code — the second half of the user's async-correctness ask.  Two patterns:

* **Event-loop re-entry bridge** — ``asyncio.run(...)`` or any
  ``*.run_until_complete(...)`` (incl. ``loop.run_until_complete`` /
  ``asyncio.get_event_loop().run_until_complete``).  Running a new event loop from
  inside a running one is an error; ``await`` the coroutine directly.  Flagged in
  any ``async def``.

* **Blocking sync I/O** — a synchronous call that sends a request or sleeps
  (``requests.get``/``post``/…/``request``, ``urllib.request.urlopen``/
  ``urlretrieve``, ``time.sleep``, and a send on a client — a
  ``requests.Session()`` or a urllib ``build_opener()`` / ``OpenerDirector()``
  — built inline, bound to a name in the same or an enclosing function, or
  bound to a ``self.<attr>`` in the same class) and blocks the event loop
  instead of awaiting an async equivalent / offloading via
  ``App.run_in_thread()``.  Constructors that do no I/O —
  ``requests.Session()``, ``requests.adapters.HTTPAdapter()``,
  ``urllib.request.Request()``, ``build_opener()`` — and lookups such as
  ``requests.codes.get`` are not flagged.  Flagged
  in ``async def`` bodies **outside** workflow context — inside workflow methods
  the same calls are already owned by P020 (sleep) and P021 (network), so they are
  skipped here to avoid double-reporting.

* **Tree-scale filesystem work** — ``shutil.rmtree`` / ``copytree`` / ``move``,
  and the SDK's own ``SafeFileOps`` wrappers (``SafeFileOps.rmtree`` /
  ``SafeFileOps.move``; it has no ``copytree``).  These walk an unbounded
  directory tree, so their duration scales with the data, not with a fixed
  syscall cost: on the loop they stall every other coroutine — including a
  ``@task``'s auto-heartbeat, which makes Temporal retry an activity that is
  still making progress.  ``App.cleanup_files`` shipped with exactly this bug and
  nothing caught it, because this rule's inventory was network/sleep-only.

* **Tree traversal** — ``os.walk`` / ``os.scandir`` / ``glob.glob`` /
  ``glob.iglob`` and the ``Path.glob`` / ``Path.rglob`` methods.  Previously
  deferred by this module as "real, but a separate sweep"; this is that sweep.

* **Data-scale I/O** — pandas and pyarrow readers/writers (``pandas.read_sql``,
  ``read_parquet``, ``read_csv``, ``DataFrame.to_parquet``, ``pq.read_table``,
  …), whole-file ``pathlib`` accessors (``Path.read_text`` / ``write_bytes`` /
  …), file-handle (de)serialization (``json.load`` / ``json.dump`` /
  ``pickle`` / ``tomllib``), and ``subprocess.*``.  Same property as tree-scale
  FS work: duration scales with the data.

  Deliberately **not** flagged: single-syscall operations (``os.remove``,
  ``os.unlink``, ``os.rmdir``, ``os.path.*``).  One inode operation does not
  earn a thread hop, and flagging them would bury the findings that matter.
  Nor the in-memory string forms ``json.loads`` / ``json.dumps`` — they are CPU,
  not I/O, and are used for small payloads everywhere.  PyYAML and ``csv`` are
  absent for that same reason: they have no ``load``/``loads`` split, so
  matching them by name would flag string parsing, not file work.

Across that whole data-scale inventory, a call the source already marks as
async is skipped: the direct operand of ``await``, and the iterable of an
``async for``.  An ``await``-ed ``path.read_text()`` is ``anyio.Path``, not
``pathlib.Path``; ``async for p in path.glob("*")`` is an async iterator.  The
name alone cannot tell those apart, but the ``await`` / ``async for`` can.
(The event-loop bridge and the legacy ``requests`` / ``time.sleep`` patterns
are *not* skipped this way: awaiting them is meaningless, so an ``await`` there
is a bug rather than a signal.)

A ``lambda`` body is treated as a sync scope, exactly as a nested ``def`` is.
Both are the offload shape this rule prescribes — the callable runs in a
thread, not on the loop — so flagging inside them would make the prescribed
fix a finding in its own right.

Remediation is a restructure (await / run_in_thread), so findings route to residue.

The inventory below was assembled from an AST sweep of the connector fleet for
blocking calls reachable from an ``async def`` without an offload hop — every
pattern listed here had at least one real occurrence.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.orchestration._temporal_common import (
    collect_import_bindings,
)
from conformance.suite.schema.findings import Finding

from ._workflow_methods import resolve_call_target, workflow_method_nodes

RULE_ID = "P023"

_BRIDGE_EXACT = frozenset({"asyncio.run"})
_BRIDGE_ATTR = "run_until_complete"
_BLOCKING_EXACT = frozenset({"time.sleep"})
# Only the calls that send a request. Constructors (`requests.Session`,
# `requests.adapters.HTTPAdapter`, `urllib.request.Request`, ...) do no I/O:
# connections open lazily on the first send. Matched on the root plus the last
# segment, because `import urllib.request` binds `urllib` to `urllib.request`
# and the resolved target repeats the submodule.
_REQUESTS_VERBS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request"}
)
_REQUESTS_VERB_TARGETS = frozenset(
    f"{module}.{verb}"
    for module in ("requests", "requests.api")
    for verb in _REQUESTS_VERBS
)
_URLLIB_BLOCKING = frozenset({"urlopen", "urlretrieve"})
# A client whose construction does no I/O, and the methods that send on it.
_SESSION_FACTORIES = frozenset({"Session", "session"})
_OPENER_FACTORIES = frozenset({"build_opener", "OpenerDirector"})
_CLIENT_SENDS = {
    "session": _REQUESTS_VERBS | {"send"},
    "opener": frozenset({"open"}),
}
_CLIENT_LABEL = {
    "session": "requests.Session()",
    "opener": "urllib.request.build_opener()",
}


def _is_blocking_network(target: str) -> bool:
    return target in _REQUESTS_VERB_TARGETS or (
        target.startswith("urllib.request.")
        and target.rsplit(".", 1)[-1] in _URLLIB_BLOCKING
    )


def _client_kind(target: str | None) -> str | None:
    if target is None:
        return None
    last = target.rsplit(".", 1)[-1]
    if target.startswith("requests.") and last in _SESSION_FACTORIES:
        return "session"
    if target.startswith("urllib.request.") and last in _OPENER_FACTORIES:
        return "opener"
    return None


def _assignment_pairs(stmt: ast.AST) -> list[tuple[ast.expr, ast.expr | None]]:
    if isinstance(stmt, ast.Assign):
        return [(target, stmt.value) for target in stmt.targets]
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        return [(stmt.target, stmt.value)]
    if isinstance(stmt, ast.With):
        return [
            (item.optional_vars, item.context_expr)
            for item in stmt.items
            if item.optional_vars is not None
        ]
    return []


def _walk_same_class(node: ast.AST):
    """``ast.walk`` that does not descend into a nested class."""
    pending = list(ast.iter_child_nodes(node))
    while pending:
        current = pending.pop()
        if isinstance(current, ast.ClassDef):
            continue
        yield current
        pending.extend(ast.iter_child_nodes(current))


# Tree-scale filesystem work: duration scales with the tree, not with a fixed
# syscall cost. Single-inode ops (os.remove / os.unlink / os.rmdir) are
# intentionally absent — see the module docstring.
_TREE_FS_OPS = frozenset({"rmtree", "copytree", "move"})
_TREE_FS_EXACT = frozenset(f"shutil.{op}" for op in _TREE_FS_OPS)
# The SDK's own wrappers over the same calls, so routing through SafeFileOps is
# not a way around this rule. SafeFileOps wraps only rmtree and move (its `copy`
# is single-file, and there is no `copytree` wrapper) — so the wrapper set is a
# strict subset of _TREE_FS_OPS. Patterns are anchored on the segment boundary
# (a leading dot, or the bare name) so a look-alike class such as
# `MySafeFileOps` does not match.
_SAFE_FILE_OPS_WRAPPED = frozenset({"rmtree", "move"})
_TREE_FS_WRAPPER_SUFFIXES = tuple(f".SafeFileOps.{op}" for op in _SAFE_FILE_OPS_WRAPPED)
_TREE_FS_WRAPPER_BARE = frozenset(f"SafeFileOps.{op}" for op in _SAFE_FILE_OPS_WRAPPED)

# Tree traversal: the sweep this module's docstring previously deferred. Cost
# scales with the number of entries walked, so it belongs with the tree-scale
# set rather than with single-inode ops.
_TRAVERSAL_EXACT = frozenset({"os.walk", "os.scandir", "glob.glob", "glob.iglob"})
# `Path.glob` / `Path.rglob` are called on a local, so `resolve_call_target`
# yields `<local>.glob` -- matched on the trailing segment. `ast.walk` is the
# reason a bare `walk` is NOT in this set: it is not I/O and it is everywhere.
_TRAVERSAL_SUFFIXES = (".glob", ".rglob")

# Data-scale I/O: pandas/pyarrow readers and writers. Module-qualified, so the
# import bindings resolve them (`import pandas as pd` -> `pandas.read_sql`).
_DATA_IO_EXACT = frozenset(
    {
        f"pandas.{fn}"
        for fn in (
            "read_sql",
            "read_sql_query",
            "read_sql_table",
            "read_csv",
            "read_parquet",
            "read_json",
            "read_excel",
        )
    }
    | {
        f"pyarrow.parquet.{fn}"
        for fn in ("read_table", "write_table", "write_to_dataset", "write_dataset")
    }
    | {f"pyarrow.{fn}" for fn in ("memory_map",)}
)
# The writer half is called on a DataFrame instance (`df.to_parquet(...)`), so
# it can only be matched on the trailing segment.
_DATA_IO_SUFFIXES = (".to_parquet", ".to_csv", ".to_sql", ".to_excel")

# Whole-file pathlib accessors: the entire file is read/written in one call.
# `.open()` is absent -- opening is a single syscall; it is the read that scales.
_WHOLE_FILE_SUFFIXES = (
    ".read_text",
    ".read_bytes",
    ".write_text",
    ".write_bytes",
)

# (De)serialization against a file handle. Only APIs whose *name* guarantees a
# file object: `json.load` takes a stream and `json.loads` takes a string, so
# matching the former never touches in-memory work.
#
# PyYAML and `csv` are deliberately absent even though they are the same class
# of cost, because they have no such split -- `yaml.safe_load` and `csv.reader`
# each accept a string/iterable as readily as a handle. Matching them by name
# would flag exactly the in-memory parsing that `json.loads` is deliberately
# allowed to do, and neither appeared in the fleet sweep. They come back if a
# file-handle heuristic (argument is an `open(...)` result or a `with` target)
# is ever worth the machinery.
_SERIALIZE_EXACT = frozenset(
    {
        "json.load",
        "json.dump",
        "pickle.load",
        "pickle.dump",
        "tomllib.load",
        "toml.load",
    }
)

_SUBPROCESS_EXACT = frozenset(
    {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "os.system",
        "os.popen",
    }
)


def is_data_scale_io(target: str) -> bool:
    """True if *target* is data-scale I/O (P023 outside workflow context, P021 inside)."""
    return (
        target in _TREE_FS_EXACT
        or target.endswith(_TREE_FS_WRAPPER_SUFFIXES)
        or target in _TREE_FS_WRAPPER_BARE
        or target in _SERIALIZE_EXACT
        or target in _DATA_IO_EXACT
        or target in _TRAVERSAL_EXACT
        or target.endswith(_DATA_IO_SUFFIXES)
        or target.endswith(_WHOLE_FILE_SUFFIXES)
        or target.endswith(_TRAVERSAL_SUFFIXES)
    )


_BRIDGE_HINT = (
    "Running an event loop from inside an async function re-enters the loop and "
    "deadlocks/raises. Await the coroutine directly instead."
)
_BLOCKING_HINT = (
    "This blocks the event loop. Await an async equivalent, or offload it with "
    "App.run_in_thread() inside a @task."
)
_TREE_FS_HINT = (
    "This walks an unbounded directory tree, so it blocks the event loop for as "
    "long as the tree takes to process — starving a @task's auto-heartbeat and "
    "making Temporal retry an activity that is still making progress. Offload it: "
    "await run_in_thread(shutil.rmtree, path) (application_sdk.execution.heartbeat) "
    "or self.task_context.run_in_thread(...) inside a @task."
)
_TRAVERSAL_HINT = (
    "Directory traversal costs one syscall per entry, so it blocks the event "
    "loop in proportion to the tree — the same starvation as a tree-scale "
    "delete. Offload it: await run_in_thread(lambda: list(path.rglob('*'))) "
    "(application_sdk.execution.heartbeat), or self.task_context.run_in_thread(...) "
    "inside a @task. Materialise the iterator inside the thread; returning a "
    "lazy generator moves the work straight back onto the loop."
)
_DATA_IO_HINT = (
    "This reads or writes the whole dataset synchronously, so it blocks the "
    "event loop for as long as the data takes — starving a @task's "
    "auto-heartbeat. Offload it: await run_in_thread(pd.read_parquet, path) "
    "(application_sdk.execution.heartbeat) or self.task_context.run_in_thread(...) "
    "inside a @task."
)
_WHOLE_FILE_HINT = (
    "This reads or writes the entire file in one blocking call, so it stalls the "
    "event loop in proportion to the file size. Offload it with "
    "run_in_thread (application_sdk.execution.heartbeat), or stream it through "
    "the SDK's object-store APIs."
)
_SERIALIZE_HINT = (
    "Parsing or writing a whole file blocks the event loop in proportion to its "
    "size. Offload it: await run_in_thread(json.load, handle) "
    "(application_sdk.execution.heartbeat) or self.task_context.run_in_thread(...) "
    "inside a @task."
)
_SUBPROCESS_HINT = (
    "A synchronous subprocess call blocks the event loop until the child exits. "
    "Use asyncio.create_subprocess_exec/_shell and await it, or offload the "
    "blocking call with run_in_thread (application_sdk.execution.heartbeat)."
)


class _Visitor(ast.NodeVisitor):
    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        bindings: dict[str, str],
        workflow_ids: frozenset[int],
    ) -> None:
        self.filename = filename
        self.directives = directives
        self.bindings = bindings
        self.workflow_ids = workflow_ids
        self._async_stack: list[bool] = []
        self._scopes: list[dict[str, str | None]] = []
        self._class_clients: list[dict[str, str]] = []
        self._class_floors: list[int] = []
        self._wf_depth = 0
        self._awaited: set[int] = set()
        self.findings: list[Finding] = []

    def visit_Await(self, node: ast.Await) -> None:
        # An awaited call is an async API wearing a sync-looking name:
        # `await path.read_text()` is anyio.Path, `await cur.fetchall()` is an
        # async driver. Record it so the data-scale matches skip it.
        if isinstance(node.value, ast.Call):
            self._awaited.add(id(node.value))
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        # `async for p in path.glob("*")` is an async iterator, same argument
        # as `await` — the `async for` is what marks it, since the call itself
        # is never an Await operand.
        if isinstance(node.iter, ast.Call):
            self._awaited.add(id(node.iter))
        self.generic_visit(node)

    # No `visit_AsyncWith`: nothing in the inventory is plausible as an async
    # context expression. The idiom that would need one is
    # `async with aiofiles.open(p)`, and `.open` is deliberately absent from
    # _WHOLE_FILE_SUFFIXES (opening is a single syscall; it is the read that
    # scales). A guard with no matchable witness can only be tested vacuously,
    # so it is left out until an inventory entry earns it.

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # A lambda body is a separate function scope that runs when the lambda
        # is called — which, for the offload shape this rule prescribes, is in
        # a thread. Treating it as sync is the same judgement already made for
        # a nested `def` (visit_FunctionDef), and without it the fix
        # _TRAVERSAL_HINT asks for --
        # `await run_in_thread(lambda: list(path.rglob("*")))` -- is itself
        # flagged, which would make the rule un-satisfiable.
        self._visit_func(node, is_async=False)

    def _visit_func(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        is_async: bool,
    ) -> None:
        in_wf = id(node) in self.workflow_ids
        self._async_stack.append(is_async)
        args = node.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        params += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
        self._scopes.append({param.arg: None for param in params})
        if in_wf:
            self._wf_depth += 1
        self.generic_visit(node)
        if in_wf:
            self._wf_depth -= 1
        self._scopes.pop()
        self._async_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_clients.append(self._self_clients(node))
        self._class_floors.append(len(self._scopes))
        self.generic_visit(node)
        self._class_floors.pop()
        self._class_clients.pop()

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        self._scopes.append({})
        for generator in node.generators:
            self.visit(generator)
        results = (
            [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        )
        for result in results:
            self.visit(result)
        self._scopes.pop()

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = (
        _visit_comprehension
    )

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store) and self._scopes:
            self._scopes[-1][node.id] = None

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.generic_visit(node)
        self._bind(node.target, node.value)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name and self._scopes:
            self._scopes[-1][node.name] = None
        self.generic_visit(node)

    def _self_clients(self, node: ast.ClassDef) -> dict[str, str]:
        """``self.<attr>`` names bound only to one kind of client in this class.

        A name also bound to anything else (``None`` aside, the lazy-init idiom)
        is dropped: the class does not say which value a send reaches.
        """
        kinds: dict[str, set[str | None]] = {}
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for stmt in _walk_same_class(method):
                for target, value in _assignment_pairs(stmt):
                    key = self._binding_key(target)
                    if not key or not key.startswith("self."):
                        continue
                    if isinstance(value, ast.Constant) and value.value is None:
                        continue
                    kinds.setdefault(key, set()).add(self._client_call(value))
        clients: dict[str, str] = {}
        for key, found in kinds.items():
            kind = next(iter(found)) if len(found) == 1 else None
            if kind is not None:
                clients[key] = kind
        return clients

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node, is_async=True)

    def _client_call(self, value: ast.expr | None) -> str | None:
        if not isinstance(value, ast.Call):
            return None
        return _client_kind(resolve_call_target(value.func, self.bindings))

    def _binding_key(self, target: ast.expr) -> str | None:
        if not isinstance(target, (ast.Name, ast.Attribute)):
            return None
        return resolve_call_target(target, self.bindings)

    def _bind(self, target: ast.expr, value: ast.expr | None) -> None:
        key = self._binding_key(target)
        if self._scopes and key is not None:
            self._scopes[-1][key] = self._client_call(value)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        for target in node.targets:
            self._bind(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if node.value is not None:
            self._bind(node.target, node.value)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind(item.optional_vars, item.context_expr)
        for stmt in node.body:
            self.visit(stmt)

    def _receiver_kind(self, receiver: str) -> str | None:
        """The client bound to ``receiver``, innermost scope first, then the class.

        A plain name follows Python's closures through every enclosing function.
        A ``self.<attr>`` stops at the nearest class: its ``self`` is that
        class's instance, not the one an enclosing method bound.
        """
        floor = 0
        if receiver.startswith("self.") and self._class_floors:
            floor = self._class_floors[-1]
        for scope in reversed(self._scopes[floor:]):
            if receiver in scope:
                return scope[receiver]
        if self._class_clients:
            return self._class_clients[-1].get(receiver)
        return None

    def _is_named_client_send(self, target: str) -> bool:
        receiver, _, attr = target.rpartition(".")
        kind = self._receiver_kind(receiver)
        return kind is not None and attr in _CLIENT_SENDS[kind]

    def _in_async(self) -> bool:
        return bool(self._async_stack) and self._async_stack[-1]

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_async():
            self._check_call(node)
        self.generic_visit(node)

    def _check_call(self, node: ast.Call) -> None:
        # Event-loop re-entry bridge — flagged everywhere, incl. workflow context.
        if isinstance(node.func, ast.Attribute) and node.func.attr == _BRIDGE_ATTR:
            self._add(node, f".{_BRIDGE_ATTR}()", _BRIDGE_HINT)
            return
        target = resolve_call_target(node.func, self.bindings)
        if target is None:
            label = self._inline_client_send(node)
            if self._wf_depth == 0 and label is not None:
                self._add(node, label, _BLOCKING_HINT)
            return
        if target in _BRIDGE_EXACT:
            self._add(node, f"{target}()", _BRIDGE_HINT)
            return
        # Blocking sync I/O — skip inside workflow context (P020/P021 own it).
        if self._wf_depth == 0 and (
            target in _BLOCKING_EXACT
            or _is_blocking_network(target)
            or self._is_named_client_send(target)
        ):
            self._add(node, f"{target}()", _BLOCKING_HINT)
            return
        # Everything below is data-scale work, and all of it is also P021's
        # territory inside workflow context (file I/O belongs in a @task at
        # all), so it is reported outside workflow methods only.
        if self._wf_depth != 0:
            return
        # `await`-ed calls are excluded from the whole data-scale inventory,
        # not just its suffix half: `await pd.read_parquet(...)` is a wrapper
        # returning a coroutine, and `async for p in path.glob("*")` is an
        # async iterator. The name alone cannot separate those from their
        # blocking namesakes; the await can. Checked here, before the first
        # match, so the exclusion the docstring promises actually holds.
        if id(node) in self._awaited:
            return
        # Tree-scale filesystem work.
        if (
            target in _TREE_FS_EXACT
            or target.endswith(_TREE_FS_WRAPPER_SUFFIXES)
            or target in _TREE_FS_WRAPPER_BARE
        ):
            self._add(node, f"{target}()", _TREE_FS_HINT)
            return
        if target in _SUBPROCESS_EXACT:
            self._add(node, f"{target}()", _SUBPROCESS_HINT)
            return
        if target in _SERIALIZE_EXACT:
            self._add(node, f"{target}()", _SERIALIZE_HINT)
            return
        if target in _DATA_IO_EXACT:
            self._add(node, f"{target}()", _DATA_IO_HINT)
            return
        # Exact matches are checked first so `glob.glob` is not also caught by
        # the `.glob` suffix and reported twice.
        if target in _TRAVERSAL_EXACT:
            self._add(node, f"{target}()", _TRAVERSAL_HINT)
            return
        # Instance-method forms, matched on the trailing segment because the
        # receiver is a local (`df.to_parquet()`, `path.read_text()`).
        if target.endswith(_DATA_IO_SUFFIXES):
            self._add(node, f"{target}()", _DATA_IO_HINT)
            return
        if target.endswith(_WHOLE_FILE_SUFFIXES):
            self._add(node, f"{target}()", _WHOLE_FILE_HINT)
            return
        if target.endswith(_TRAVERSAL_SUFFIXES):
            self._add(node, f"{target}()", _TRAVERSAL_HINT)

    def _inline_client_send(self, node: ast.Call) -> str | None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        kind = self._client_call(func.value)
        if kind is None or func.attr not in _CLIENT_SENDS[kind]:
            return None
        return f"{_CLIENT_LABEL[kind]}.{func.attr}()"

    def _add(self, node: ast.Call, label: str, hint: str) -> None:
        self.findings.append(
            make_finding(
                filename=self.filename,
                rule_id=RULE_ID,
                node=node,
                message=f"Blocking call '{label}' in an async function. {hint}",
                directives=self.directives,
            )
        )


def check_p023(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit P023 findings for event-loop bridges and blocking sync I/O in async defs."""
    bindings = collect_import_bindings(tree)
    workflow_ids = frozenset(id(n) for n in workflow_method_nodes(tree))
    visitor = _Visitor(filename, directives, bindings, workflow_ids)
    visitor.visit(tree)
    return visitor.findings
