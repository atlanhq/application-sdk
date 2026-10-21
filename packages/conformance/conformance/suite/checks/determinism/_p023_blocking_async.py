"""P023 — BlockingCallInAsyncDef.

Enforces "use ``await``, not a sync bridge or blocking lib" inside ``async def``
code — the second half of the user's async-correctness ask.  Patterns:

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

* **Client and handle work** — the same client tracking as ``requests.Session()``
  extended to the clients connector code actually holds: a sync
  ``httpx.Client()``, a ``boto3`` client or resource (every operation —
  boto3 generates them from service models, so only the no-I/O ones such as
  ``generate_presigned_url`` are exempt), a handle from ``open()`` /
  ``gzip.open()`` (its ``read`` / ``write``), a ``zipfile.ZipFile`` /
  ``tarfile`` archive, a ``threading.Event`` / ``Thread`` wait, a
  ``Future.result()`` from ``executor.submit``, a sqlalchemy engine or
  connection ``execute``, and a DuckDB connection. Plus the module-level
  calls: ``httpx.get`` / …, ``os.listdir`` / ``Path.iterdir``,
  ``shutil.copyfile`` / ``copy`` / ``copy2``, ``gzip.compress`` /
  ``decompress``, ``os.fsync``, ``concurrent.futures.wait``, ``duckdb.sql``.

* **One hop into a sync helper** — a call from an ``async def`` to a sync
  function this repo defines (a top-level function in the same module, a sync
  method of the same class called through ``self``, or a function imported
  from a module under the scanned root) is reported when that function's own
  body makes a call this rule flags. Exactly one hop: a helper's helpers are
  not followed, which keeps the rule linear in file size and the finding
  pointed at a call the reader can see. A suppression inside the helper
  counts, and passing the helper to ``run_in_thread`` is not a call.

A wholesale-deprecated module (a top-level ``DeprecationWarning``, on the file
or an enclosing package) is skipped: it is being removed, not fixed.

Across that whole data-scale inventory, a call the source already marks as
async is skipped: the direct operand of ``await``, and the iterable of an
``async for``.  An ``await``-ed ``path.read_text()`` is ``anyio.Path``, not
``pathlib.Path``; ``async for p in path.glob("*")`` is an async iterator.  The
name alone cannot tell those apart, but the ``await`` / ``async for`` can.
(The event-loop bridge, the ``requests`` / ``time.sleep`` patterns and a send
on a tracked client are *not* skipped this way: the callee is already known to
be sync, so the call blocks before ``await`` fails on its result — an
``await`` there is a bug rather than a signal.)

A ``lambda`` body is treated as a sync scope, exactly as a nested ``def`` is.
Both are the offload shape this rule prescribes — the callable runs in a
thread, not on the loop — so flagging inside them would make the prescribed
fix a finding in its own right.

Remediation is a restructure (await / run_in_thread), so findings route to residue.

The inventory below was assembled from an AST sweep of the connector fleet for
blocking calls reachable from an ``async def`` without an offload hop — every
pattern listed here had at least one real occurrence. The client/handle work
and the helper hop came from the FND-3936 SDK audit, which found the
name-only inventory missing most activity-path blocking sites.
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
from conformance.suite.checks._deprecated_module import module_is_deprecated
from conformance.suite.checks._deprecation_warn import _class_warn_message
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
# connections open lazily on the first send. Targets resolve against
# `_module_bindings`, so `import requests.api` does not repeat the submodule.
_REQUESTS_VERBS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request"}
)
_REQUESTS_VERB_TARGETS = frozenset(
    f"{module}.{verb}"
    for module in ("requests", "requests.api")
    for verb in _REQUESTS_VERBS
)
_URLLIB_BLOCKING = frozenset({"urlopen", "urlretrieve"})
# httpx's module-level helpers each build a throwaway sync Client and send.
_HTTPX_VERB_TARGETS = frozenset(
    f"httpx.{verb}" for verb in (*_REQUESTS_VERBS, "stream")
)
# A client whose construction does no I/O, and the methods that send on it.
_SESSION_FACTORIES = frozenset({"Session", "session"})
_OPENER_FACTORIES = frozenset({"build_opener", "OpenerDirector"})


def _is_blocking_network(target: str) -> bool:
    return (
        target in _REQUESTS_VERB_TARGETS
        or target in _HTTPX_VERB_TARGETS
        or (
            target.startswith("urllib.request.")
            and target.rsplit(".", 1)[-1] in _URLLIB_BLOCKING
        )
    )


def _module_bindings(tree: ast.AST) -> dict[str, str]:
    """Import bindings with a plain ``import a.b`` bound the way Python binds it.

    The shared ``collect_import_bindings`` maps ``a`` to ``a.b``, so the call
    ``a.b.f()`` resolves to ``a.b.b.f`` and ``a.f()`` to ``a.b.f`` — neither is
    the function called. ``import a.b`` binds ``a`` to the package ``a``.
    Only a name whose final binding is that dotted import is rewritten, so a
    later ``from x import a`` keeps its own origin.
    """
    bindings = collect_import_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if alias.asname is None and bindings.get(root) == alias.name != root:
                    bindings[root] = root
    return bindings


def _client_kind(target: str | None) -> str | None:
    if target is None:
        return None
    if target in _CLIENT_FACTORIES:
        return _CLIENT_FACTORIES[target]
    last = target.rsplit(".", 1)[-1]
    if target.startswith("requests.") and last in _SESSION_FACTORIES:
        return "session"
    if target.startswith("urllib.request.") and last in _OPENER_FACTORIES:
        return "opener"
    if _safe_file_op(target) == "open":
        return "file"
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
# The SDK's own wrappers over the calls this rule flags, so routing through
# SafeFileOps is not a way around it. Each wrapped op maps to the inventory it
# wraps: rmtree/move are tree-scale, copy is a whole-file copy (as
# shutil.copyfile is), listdir is a traversal, and open hands back a file
# handle. There is no `copytree` wrapper. Matching is anchored on the segment
# boundary (a leading dot, or the bare name) so a look-alike class such as
# `MySafeFileOps` does not match.
_SAFE_FILE_OPS = {
    "rmtree": "tree",
    "move": "tree",
    "copy": "file",
    "listdir": "traversal",
    "open": "handle",
}


def _safe_file_op(target: str) -> str | None:
    owner, _, op = target.rpartition(".")
    if op in _SAFE_FILE_OPS and (
        owner == "SafeFileOps" or owner.endswith(".SafeFileOps")
    ):
        return op
    return None


# Tree traversal: the sweep this module's docstring previously deferred. Cost
# scales with the number of entries walked, so it belongs with the tree-scale
# set rather than with single-inode ops.
_TRAVERSAL_EXACT = frozenset(
    {"os.walk", "os.scandir", "os.listdir", "glob.glob", "glob.iglob"}
)
# `Path.glob` / `Path.rglob` / `Path.iterdir` are called on a local, so
# `resolve_call_target` yields `<local>.glob` -- matched on the trailing
# segment. `ast.walk` is the reason a bare `walk` is NOT in this set: it is not
# I/O and it is everywhere.
_TRAVERSAL_SUFFIXES = (".glob", ".rglob", ".iterdir")

# Whole-file and whole-buffer work: one call copies, (de)compresses or archives
# the entire input, so its cost scales with the input like a whole-file read.
_FILE_SCALE_EXACT = frozenset(
    {
        "shutil.copyfile",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfileobj",
        "shutil.make_archive",
        "shutil.unpack_archive",
    }
    | {
        f"{codec}.{fn}"
        for codec in ("gzip", "bz2", "lzma")
        for fn in ("compress", "decompress")
    }
)

# Durability barriers: they return when the device acknowledges the write, which
# on a busy or network volume is seconds, not a fixed syscall cost.
_FSYNC_EXACT = frozenset({"os.fsync", "os.fdatasync", "os.sync"})

# Waiting on another thread's work by module function. The method forms
# (`Event.wait`, `Future.result`, `Thread.join`) are client sends below.
_WAIT_EXACT = frozenset({"concurrent.futures.wait", "concurrent.futures.as_completed"})

# DuckDB's module-level API runs on a process-default connection.
_QUERY_EXACT = frozenset(
    f"duckdb.{fn}"
    for fn in ("sql", "query", "execute", "read_parquet", "read_csv", "read_json")
)

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
        or _safe_file_op(target) not in (None, "open")
        or target in _SERIALIZE_EXACT
        or target in _DATA_IO_EXACT
        or target in _TRAVERSAL_EXACT
        or target in _FILE_SCALE_EXACT
        or target in _FSYNC_EXACT
        or target in _QUERY_EXACT
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
_FILE_SCALE_HINT = (
    "This copies, (de)compresses or archives a whole file or buffer in one "
    "blocking call, so it stalls the event loop in proportion to its size. "
    "Offload it with run_in_thread (application_sdk.execution.heartbeat)."
)
_FSYNC_HINT = (
    "fsync returns when the device acknowledges the write — seconds on a busy or "
    "network volume — and blocks the event loop until then. Offload it with "
    "run_in_thread (application_sdk.execution.heartbeat), together with the "
    "write it makes durable."
)
_WAIT_HINT = (
    "This parks the calling thread until another thread finishes — on the event "
    "loop, that stops every coroutine, including a @task's auto-heartbeat. Use "
    "the asyncio equivalent (asyncio.Event, await asyncio.wrap_future(future)), "
    "or offload the wait with run_in_thread (application_sdk.execution.heartbeat)."
)
_QUERY_HINT = (
    "This runs a query synchronously, so it blocks the event loop for as long as "
    "the database takes. Offload the connect-and-query block with run_in_thread "
    "(application_sdk.execution.heartbeat), or use the driver's async API."
)


@dataclass(frozen=True)
class _Client:
    """A client whose construction does no I/O, and the methods that do.

    ``sends`` of ``None`` means every method except ``exempt`` and the
    capitalised sub-resource constructors (``s3.Bucket(...)``): boto3 builds
    its operations from service models at runtime, so they cannot be listed.
    A send is reported even when awaited: the receiver is already known to be
    sync, so the call blocks before ``await`` fails on its result.
    """

    label: str
    hint: str
    sends: frozenset[str] | None
    exempt: frozenset[str] = frozenset()

    def is_send(self, method: str) -> bool:
        if self.sends is not None:
            return method in self.sends
        return method not in self.exempt and not method[:1].isupper()


_HANDLE_IO = frozenset({"read", "readlines", "write", "writelines"})
_CLIENTS = {
    "session": _Client(
        "requests.Session()", _BLOCKING_HINT, _REQUESTS_VERBS | {"send"}
    ),
    "opener": _Client(
        "urllib.request.build_opener()",
        _BLOCKING_HINT,
        frozenset({"open"}),
    ),
    "httpx": _Client(
        "httpx.Client()", _BLOCKING_HINT, _REQUESTS_VERBS | {"send", "stream"}
    ),
    "boto3": _Client(
        "boto3.client()",
        _BLOCKING_HINT,
        None,
        exempt=frozenset(
            {
                "can_paginate",
                "close",
                "generate_presigned_post",
                "generate_presigned_url",
                "get_paginator",
                "get_waiter",
            }
        ),
    ),
    # Only hands out clients (see _DERIVED); sends nothing itself.
    "boto3_session": _Client("boto3.Session()", _BLOCKING_HINT, frozenset()),
    "file": _Client("open()", _WHOLE_FILE_HINT, _HANDLE_IO),
    "archive": _Client(
        "zipfile.ZipFile()",
        _FILE_SCALE_HINT,
        frozenset(
            {"extractall", "extract", "write", "writestr", "read", "add", "testzip"}
        ),
    ),
    "event": _Client("threading.Event()", _WAIT_HINT, frozenset({"wait", "wait_for"})),
    "thread": _Client("threading.Thread()", _WAIT_HINT, frozenset({"join"})),
    "executor": _Client("ThreadPoolExecutor()", _WAIT_HINT, frozenset()),
    "future": _Client("Future", _WAIT_HINT, frozenset({"result", "exception"})),
    "sa_engine": _Client("create_engine()", _QUERY_HINT, frozenset({"execute"})),
    "sa_conn": _Client(
        "Connection",
        _QUERY_HINT,
        frozenset({"execute", "exec_driver_sql", "scalar", "scalars", "commit"}),
    ),
    "duckdb": _Client(
        "duckdb.connect()",
        _QUERY_HINT,
        frozenset(
            {
                "execute",
                "executemany",
                "sql",
                "query",
                "fetchall",
                "fetchone",
                "fetchmany",
                "fetchdf",
                "fetch_df",
                "df",
                "arrow",
                "fetch_arrow_table",
                "pl",
                "read_parquet",
                "read_csv",
                "read_json",
            }
        ),
    ),
}
_CLIENT_FACTORIES = {
    "httpx.Client": "httpx",
    "boto3.client": "boto3",
    "boto3.resource": "boto3",
    "boto3.Session": "boto3_session",
    "boto3.session.Session": "boto3_session",
    "open": "file",
    "io.open": "file",
    "codecs.open": "file",
    "gzip.open": "file",
    "bz2.open": "file",
    "lzma.open": "file",
    "zipfile.ZipFile": "archive",
    "tarfile.open": "archive",
    "tarfile.TarFile": "archive",
    "threading.Event": "event",
    "threading.Condition": "event",
    "threading.Barrier": "event",
    "threading.Thread": "thread",
    "concurrent.futures.ThreadPoolExecutor": "executor",
    "concurrent.futures.ProcessPoolExecutor": "executor",
    "asyncio.run_coroutine_threadsafe": "future",
    "sqlalchemy.create_engine": "sa_engine",
    "sqlalchemy.engine.create_engine": "sa_engine",
    "duckdb.connect": "duckdb",
}
# A method that hands back another client rather than doing the I/O itself.
# Not a send: `engine.connect()` pairs with the `execute` that follows it, and
# reporting both would double-count one block.
_DERIVED = {
    ("boto3_session", "client"): "boto3",
    ("boto3_session", "resource"): "boto3",
    ("executor", "submit"): "future",
    ("sa_engine", "connect"): "sa_conn",
    ("sa_engine", "begin"): "sa_conn",
}
_PATH_CONSTRUCTORS = frozenset({"pathlib.Path", "pathlib.PurePath"})


@dataclass
class _Module:
    """What a visitor needs to scan one source file."""

    filename: str
    tree: ast.Module
    directives: dict[int, _IgnoreDirective]
    bindings: dict[str, str]
    path: Path | None

    @classmethod
    def of(
        cls,
        tree: ast.Module,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        path: Path | None,
    ) -> _Module:
        return cls(filename, tree, directives, _module_bindings(tree), path)

    @cached_property
    def functions(self) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
        return {
            node.name: node
            for node in self.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    @cached_property
    def imports(self) -> dict[str, ast.ImportFrom]:
        return {
            alias.asname or alias.name: node
            for node in self.tree.body
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }


@dataclass(frozen=True)
class _Reached:
    """The first unsuppressed call a sync helper's body makes that P023 flags."""

    label: str
    hint: str
    filename: str
    line: int


class _Helpers:
    """One-hop helper resolution, shared by every visitor of one scan."""

    def __init__(self, module: _Module, root: Path | None) -> None:
        self._module = module
        self._root = root
        self._loaded: dict[Path, _Module | None] = {}
        self._reached: dict[int, _Reached | None] = {}

    def by_name(self, name: str) -> _Reached | None:
        """A top-level function of this module, or one imported from the repo."""
        function = self._module.functions.get(name)
        if function is not None:
            return self._reach(self._module, function, None)
        node = self._module.imports.get(name)
        if node is None:
            return None
        source = repo_module_file(
            self._root, node.module, node.level, self._module.path
        )
        imported = self._load(source) if source is not None else None
        if imported is None:
            return None
        original = next(
            alias.name for alias in node.names if (alias.asname or alias.name) == name
        )
        function = imported.functions.get(original)
        return self._reach(imported, function, None) if function is not None else None

    def by_method(self, cls: ast.ClassDef, name: str) -> _Reached | None:
        """A method of *cls* called through ``self``."""
        function = next(
            (
                node
                for node in cls.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
            ),
            None,
        )
        return (
            self._reach(self._module, function, cls) if function is not None else None
        )

    def _load(self, source: Path) -> _Module | None:
        if source not in self._loaded:
            text = safe_read_text(source)
            module: _Module | None = None
            if text is not None:
                try:
                    tree = ast.parse(text, filename=str(source))
                except SyntaxError:
                    tree = None
                # A helper in a deprecated module is being removed, not fixed —
                # the same skip the module gets when it is scanned itself.
                if tree is not None and not module_is_deprecated(
                    tree, path=source, root=self._root
                ):
                    root = self._root.resolve() if self._root is not None else None
                    rel = source.relative_to(root) if root is not None else source
                    module = _Module.of(
                        tree, rel.as_posix(), _parse_directives(text), source
                    )
            self._loaded[source] = module
        return self._loaded[source]

    def _reach(
        self,
        module: _Module,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        cls: ast.ClassDef | None,
    ) -> _Reached | None:
        # An async helper is not a hop: calling it un-awaited runs nothing, and
        # its own body is scanned as an async def where it is defined.
        if isinstance(function, ast.AsyncFunctionDef):
            return None
        if id(function) not in self._reached:
            # No `helpers`: the scan of a helper never follows a further hop.
            visitor = _Visitor(module, frozenset(), helpers=None)
            visitor.scan_helper(function, cls)
            self._reached[id(function)] = next(
                (
                    _Reached(label, hint, module.filename, finding.line)
                    for finding, label, hint in visitor.hits
                    if not finding.suppressed
                ),
                None,
            )
        return self._reached[id(function)]


class _Visitor(ast.NodeVisitor):
    def __init__(
        self,
        module: _Module,
        workflow_ids: frozenset[int],
        *,
        helpers: _Helpers | None,
    ) -> None:
        self.filename = module.filename
        self.directives = module.directives
        self.bindings = module.bindings
        self.workflow_ids = workflow_ids
        self.helpers = helpers
        self._async_stack: list[bool] = []
        self._scopes: list[dict[str, str | None]] = []
        self._class_clients: list[dict[str, str]] = []
        self._class_floors: list[int] = []
        self._class_nodes: list[ast.ClassDef] = []
        self._class_bodies: set[int] = set()
        self._comprehensions: set[int] = set()
        self._wf_depth = 0
        self._awaited: set[int] = set()
        # Each finding with the label and hint it was raised for, so a helper
        # scan can report what it reached without re-parsing the message.
        self.hits: list[tuple[Finding, str, str]] = []

    @property
    def findings(self) -> list[Finding]:
        return [finding for finding, _, _ in self.hits]

    def scan_helper(self, function: ast.FunctionDef, cls: ast.ClassDef | None) -> None:
        """Scan a sync helper's body as if it ran on the loop, which it does when
        an ``async def`` calls it."""
        if cls is not None:
            self._enter_class(cls)
        self._visit_func(function, is_async=True)
        if cls is not None:
            self._leave_class()

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
        self._visit_loop(node)

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    # Loops and comprehension clauses visit the iterable before the target: it
    # is evaluated first, so `for s in s.get(u)` resolves `s` against the
    # session, not the loop variable. `generic_visit` would go target-first.
    def _visit_loop(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self.visit(node.target)
        for stmt in [*node.body, *node.orelse]:
            self.visit(stmt)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.visit(node.iter)
        self.visit(node.target)
        for condition in node.ifs:
            self.visit(condition)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        # `async with aiofiles.open(p) as fh` binds an async handle: it shadows
        # any earlier `with open(p) as fh`, so `fh.read()` after it is not a
        # sync file read. Nothing an `async with` yields is a tracked client.
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
                key = self._binding_key(item.optional_vars)
                if self._scopes and key is not None:
                    self._scopes[-1][key] = None
        for stmt in node.body:
            self.visit(stmt)

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
        # A class whose construction warns DeprecationWarning is on its removal
        # path — the same marker the deprecated-symbol manifest reads — so its
        # methods get no findings, exactly as a deprecated module gets none.
        if _class_warn_message(node) is not None:
            return
        self._enter_class(node)
        body_scope: dict[str, str | None] = {}
        self._class_bodies.add(id(body_scope))
        self._scopes.append(body_scope)
        self.generic_visit(node)
        self._scopes.pop()
        self._class_bodies.discard(id(body_scope))
        self._leave_class()

    def _enter_class(self, node: ast.ClassDef) -> None:
        self._class_clients.append(self._self_clients(node))
        self._class_floors.append(len(self._scopes))
        self._class_nodes.append(node)

    def _leave_class(self) -> None:
        self._class_nodes.pop()
        self._class_floors.pop()
        self._class_clients.pop()

    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        scope: dict[str, str | None] = {}
        self._comprehensions.add(id(scope))
        self._scopes.append(scope)
        for generator in node.generators:
            self.visit(generator)
        results = (
            [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        )
        for result in results:
            self.visit(result)
        self._scopes.pop()
        self._comprehensions.discard(id(scope))

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = (
        _visit_comprehension
    )

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store) and self._scopes:
            self._scopes[-1][node.id] = None

    # Assignments visit the value before the target: the RHS runs first, so
    # `s = s.get(u)` must resolve `s` against the session, not the new binding.
    # A walrus target binds in the scope containing any comprehensions it sits
    # in, not in the comprehension (PEP 572), so `s` from
    # `[(s := requests.Session()) for _ in xs]` outlives the comprehension.
    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        scope = next(
            (s for s in reversed(self._scopes) if id(s) not in self._comprehensions),
            None,
        )
        key = self._binding_key(node.target)
        if scope is not None and key is not None:
            scope[key] = self._client_call(node.value)

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
        """The client kind *value* builds: a factory, or a method that derives one."""
        if not isinstance(value, ast.Call):
            return None
        target = resolve_call_target(value.func, self.bindings)
        kind = _client_kind(target)
        if kind is not None or not isinstance(value.func, ast.Attribute):
            return kind
        if target is not None:
            receiver = self._receiver_kind(target.rpartition(".")[0])
        else:
            receiver = self._client_call(value.func.value)
        if receiver:
            return _DERIVED.get((receiver, value.func.attr))
        # `path.open()` / `Path(p).open()` — pathlib's open, like fsspec's,
        # hands back a sync file handle. An `.open` on a tracked client (a
        # urllib opener's send, a zip member) was decided above; `self.open`
        # is the class's own method.
        if value.func.attr == "open" and target != "self.open":
            return "file"
        return None

    def _binding_key(self, target: ast.expr) -> str | None:
        if not isinstance(target, (ast.Name, ast.Attribute)):
            return None
        return resolve_call_target(target, self.bindings)

    def _bind(self, target: ast.expr, value: ast.expr | None) -> None:
        key = self._binding_key(target)
        if self._scopes and key is not None:
            self._scopes[-1][key] = self._client_call(value)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)
        for target in node.targets:
            self._bind(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)
        if node.value is not None:
            self._bind(node.target, node.value)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
                self._bind(item.optional_vars, item.context_expr)
        for stmt in node.body:
            self.visit(stmt)

    def _receiver_kind(self, receiver: str) -> str | None:
        """The client bound to ``receiver``, innermost scope first, then the class.

        A plain name follows Python's closures through every enclosing function,
        skipping class bodies, which a method cannot see. A ``self.<attr>``
        stops at the nearest class: its ``self`` is that class's instance, not
        the one an enclosing method bound.
        """
        floor = 0
        if receiver.startswith("self.") and self._class_floors:
            floor = self._class_floors[-1]
        innermost = self._scopes[-1] if self._scopes else None
        for scope in reversed(self._scopes[floor:]):
            if id(scope) in self._class_bodies and scope is not innermost:
                continue
            if receiver in scope:
                return scope[receiver]
        if self._class_clients:
            return self._class_clients[-1].get(receiver)
        return None

    def _named_client_send(self, target: str) -> _Client | None:
        receiver, _, attr = target.rpartition(".")
        if not receiver:
            return None
        kind = self._receiver_kind(receiver)
        client = _CLIENTS[kind] if kind is not None else None
        return client if client is not None and client.is_send(attr) else None

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
            send = self._inline_client_send(node)
            if send is not None:
                label, client = send
                if self._wf_depth == 0:
                    self._add(node, label, client.hint)
                return
            # `Path(p).iterdir()` roots its chain in a call, so it never resolves;
            # name the constructor so the pathlib suffixes below can match it.
            target = self._inline_path_target(node)
            if target is None:
                return
        if target in _BRIDGE_EXACT:
            self._add(node, f"{target}()", _BRIDGE_HINT)
            return
        client = self._named_client_send(target)
        # Blocking sync I/O — skip inside workflow context (P020/P021 own it).
        if self._wf_depth == 0 and (
            target in _BLOCKING_EXACT or _is_blocking_network(target)
        ):
            self._add(node, f"{target}()", _BLOCKING_HINT)
            return
        # A send on a client already identified as sync. Not exempted when
        # awaited: the call blocks before `await` fails on its result.
        if self._wf_depth == 0 and client is not None:
            self._add(node, f"{target}()", client.hint)
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
        hint = self._data_scale_hint(target)
        if hint is not None:
            self._add(node, f"{target}()", hint)
            return
        reached = self._helper_reach(node.func)
        if reached is not None:
            self._add(
                node,
                f"{target}()",
                f"It is a sync helper that calls '{reached.label}' at "
                f"{reached.filename}:{reached.line}. {reached.hint}",
            )

    def _data_scale_hint(self, target: str) -> str | None:
        """The hint for *target* if it is in the data-scale inventory."""
        safe_op = _safe_file_op(target)
        # Tree-scale filesystem work.
        if target in _TREE_FS_EXACT or _SAFE_FILE_OPS.get(safe_op or "") == "tree":
            return _TREE_FS_HINT
        if target in _SUBPROCESS_EXACT:
            return _SUBPROCESS_HINT
        if target in _SERIALIZE_EXACT:
            return _SERIALIZE_HINT
        if target in _DATA_IO_EXACT:
            return _DATA_IO_HINT
        if target in _FILE_SCALE_EXACT or _SAFE_FILE_OPS.get(safe_op or "") == "file":
            return _FILE_SCALE_HINT
        if target in _FSYNC_EXACT:
            return _FSYNC_HINT
        if target in _WAIT_EXACT:
            return _WAIT_HINT
        if target in _QUERY_EXACT:
            return _QUERY_HINT
        # Exact matches are checked first so `glob.glob` is not also caught by
        # the `.glob` suffix and reported twice.
        if (
            target in _TRAVERSAL_EXACT
            or _SAFE_FILE_OPS.get(safe_op or "") == "traversal"
        ):
            return _TRAVERSAL_HINT
        # Instance-method forms, matched on the trailing segment because the
        # receiver is a local (`df.to_parquet()`, `path.read_text()`).
        if target.endswith(_DATA_IO_SUFFIXES):
            return _DATA_IO_HINT
        if target.endswith(_WHOLE_FILE_SUFFIXES):
            return _WHOLE_FILE_HINT
        if target.endswith(_TRAVERSAL_SUFFIXES):
            return _TRAVERSAL_HINT
        return None

    def _helper_reach(self, func: ast.expr) -> _Reached | None:
        """What a call to a repo-defined sync helper reaches, one hop deep.

        Keyed on the call's syntax, not its resolved target: an imported
        ``purge`` resolves to ``app.util.purge``, and the bare name is what
        the module's imports map.
        """
        if self.helpers is None:
            return None
        if isinstance(func, ast.Name):
            # A parameter or local of that name is not the module's function.
            if any(func.id in scope for scope in self._scopes):
                return None
            return self.helpers.by_name(func.id)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and self._class_nodes
        ):
            return self.helpers.by_method(self._class_nodes[-1], func.attr)
        return None

    def _inline_path_target(self, node: ast.Call) -> str | None:
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call)):
            return None
        constructor = resolve_call_target(func.value.func, self.bindings)
        if constructor not in _PATH_CONSTRUCTORS:
            return None
        return f"Path().{func.attr}"

    def _inline_client_send(self, node: ast.Call) -> tuple[str, _Client] | None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        kind = self._client_call(func.value)
        client = _CLIENTS[kind] if kind is not None else None
        if client is None or not client.is_send(func.attr):
            return None
        return f"{client.label}.{func.attr}()", client

    def _add(self, node: ast.Call, label: str, hint: str) -> None:
        finding = make_finding(
            filename=self.filename,
            rule_id=RULE_ID,
            node=node,
            message=f"Blocking call '{label}' in an async function. {hint}",
            directives=self.directives,
        )
        self.hits.append((finding, label, hint))


def check_p023(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
    *,
    path: Path | None = None,
    root: Path | None = None,
) -> list[Finding]:
    """Emit P023 findings for event-loop bridges and blocking sync I/O in async defs.

    *path* and *root* let the helper hop follow a ``from … import`` into the
    scanned repo, and let a deprecation marker on an enclosing package skip the
    file. Without them, only the file itself is consulted.
    """
    if not isinstance(tree, ast.Module):
        return []
    if module_is_deprecated(tree, path=path, root=root):
        return []
    module = _Module.of(tree, filename, directives, path)
    workflow_ids = frozenset(id(n) for n in workflow_method_nodes(tree))
    visitor = _Visitor(module, workflow_ids, helpers=_Helpers(module, root))
    visitor.visit(tree)
    return visitor.findings
