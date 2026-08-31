"""P023 — BlockingCallInAsyncDef.

Enforces "use ``await``, not a sync bridge or blocking lib" inside ``async def``
code — the second half of the user's async-correctness ask.  Two patterns:

* **Event-loop re-entry bridge** — ``asyncio.run(...)`` or any
  ``*.run_until_complete(...)`` (incl. ``loop.run_until_complete`` /
  ``asyncio.get_event_loop().run_until_complete``).  Running a new event loop from
  inside a running one is an error; ``await`` the coroutine directly.  Flagged in
  any ``async def``.

* **Blocking sync I/O** — a synchronous library call (``requests.*``,
  ``urllib.request.*``, ``time.sleep``) that blocks the event loop instead of
  awaiting an async equivalent / offloading via ``App.run_in_thread()``.  Flagged
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

Calls that are the direct operand of ``await`` are skipped throughout: an
``await``-ed ``path.read_text()`` is ``anyio.Path``, not ``pathlib.Path``, and
an ``await``-ed ``cursor.fetchall()`` is an async driver.  The name alone cannot
tell those apart; the ``await`` can.

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
_BLOCKING_PREFIXES = ("requests.", "urllib.request.")

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
        self._wf_depth = 0
        self._awaited: set[int] = set()
        self.findings: list[Finding] = []

    def visit_Await(self, node: ast.Await) -> None:
        # An awaited call is an async API wearing a sync-looking name:
        # `await path.read_text()` is anyio.Path, `await cur.fetchall()` is an
        # async driver. Record it so the suffix matches below skip it.
        if isinstance(node.value, ast.Call):
            self._awaited.add(id(node.value))
        self.generic_visit(node)

    def _visit_func(self, node: ast.AST, is_async: bool) -> None:
        in_wf = id(node) in self.workflow_ids
        self._async_stack.append(is_async)
        if in_wf:
            self._wf_depth += 1
        self.generic_visit(node)
        if in_wf:
            self._wf_depth -= 1
        self._async_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node, is_async=True)

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
            return
        if target in _BRIDGE_EXACT:
            self._add(node, f"{target}()", _BRIDGE_HINT)
            return
        # Blocking sync I/O — skip inside workflow context (P020/P021 own it).
        if self._wf_depth == 0 and (
            target in _BLOCKING_EXACT
            or any(target.startswith(p) for p in _BLOCKING_PREFIXES)
        ):
            self._add(node, f"{target}()", _BLOCKING_HINT)
            return
        # Everything below is data-scale work, and all of it is also P021's
        # territory inside workflow context (file I/O belongs in a @task at
        # all), so it is reported outside workflow methods only.
        if self._wf_depth != 0:
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
        # Instance-method forms. `await`-ed calls are excluded: the name alone
        # cannot separate pathlib from anyio.Path, but the `await` can.
        if id(node) in self._awaited:
            return
        if target.endswith(_DATA_IO_SUFFIXES):
            self._add(node, f"{target}()", _DATA_IO_HINT)
            return
        if target.endswith(_WHOLE_FILE_SUFFIXES):
            self._add(node, f"{target}()", _WHOLE_FILE_HINT)
            return
        if target.endswith(_TRAVERSAL_SUFFIXES):
            self._add(node, f"{target}()", _TRAVERSAL_HINT)

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
