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

  Deliberately **not** flagged: single-syscall operations (``os.remove``,
  ``os.unlink``, ``os.rmdir``, ``os.path.*``).  One inode operation does not
  earn a thread hop, and flagging them would bury the tree-scale findings that
  matter.  Tree *traversal* (``os.walk``, ``os.scandir``) is also out of scope
  for now — real, but a separate sweep.

Remediation is a restructure (await / run_in_thread), so findings route to residue.
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
        self.findings: list[Finding] = []

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
        # Tree-scale filesystem work — also P021's territory inside workflow
        # context (file I/O belongs in a @task at all), so skip it there.
        if self._wf_depth == 0 and (
            target in _TREE_FS_EXACT
            or target.endswith(_TREE_FS_WRAPPER_SUFFIXES)
            or target in _TREE_FS_WRAPPER_BARE
        ):
            self._add(node, f"{target}()", _TREE_FS_HINT)

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
