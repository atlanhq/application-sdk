"""P031 — SharedDefaultExecutorOffload.

Flags thread offloads that land on asyncio's **shared default executor** instead
of the SDK's dedicated ``run_in_thread()`` pool:

* ``asyncio.to_thread(...)`` — always dispatches onto the default executor; there
  is no way to pass a different one.
* ``loop.run_in_executor(None, ...)`` / ``asyncio.get_event_loop().run_in_executor(None, ...)``
  — the ``None`` executor argument means "use the default executor."

Temporal's Python SDK uses that same default executor internally for its own
scheduling (see the ``_BLOCKING_EXECUTOR`` comment in
``application_sdk/execution/heartbeat.py``).  Sharing it with long-running
blocking calls can exhaust the pool and deadlock the worker.  The SDK exposes a
dedicated escape hatch, ``run_in_thread()`` (``App.run_in_thread()`` /
``self.task_context.run_in_thread()``), which dispatches onto its own
``sdk-blocking-*`` ``ThreadPoolExecutor`` specifically to avoid this.  A prior fix
in ``storage/reference.py`` shipped with a raw ``asyncio.to_thread(...)`` call
that nothing caught in review; this rule closes that gap.

This deliberately does **not** flag ``run_in_executor(<some-executor>, ...)`` when
the executor is anything other than the ``None`` literal — a call-site-owned
``ThreadPoolExecutor`` (e.g. ``clients/sql.py``'s per-connection pool) is not the
shared-pool contention this rule is about.

``execution/heartbeat.py`` itself is exempt: that is where ``run_in_thread()``'s
own dedicated-executor dispatch lives, and it never calls into the shared default
executor (it always names ``_BLOCKING_EXECUTOR`` explicitly).
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.orchestration._temporal_common import (
    collect_import_bindings,
)
from conformance.suite.schema.findings import Finding

from ._workflow_methods import resolve_call_target

RULE_ID = "P031"

_TO_THREAD_EXACT = frozenset({"asyncio.to_thread"})
_EXECUTOR_ATTR = "run_in_executor"

# The one file allowed to touch the shared default executor's counterpart: this is
# where run_in_thread()'s own dedicated-executor dispatch lives.
_EXEMPT_SUFFIX = "execution/heartbeat.py"

_HINT = (
    "This offloads onto asyncio's shared default executor, which Temporal's "
    "Python SDK also uses internally for its own scheduling — sharing it can "
    "exhaust the pool and deadlock the worker. Use run_in_thread() / "
    "App.run_in_thread() / self.task_context.run_in_thread() instead, which "
    "dispatches onto the SDK's dedicated thread pool."
)


def _is_none_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _executor_arg_is_none(node: ast.Call) -> bool:
    """True if *node* is a ``run_in_executor(...)`` call whose executor is ``None``.

    The executor is the first positional argument, or the ``executor`` keyword.
    """
    if node.args:
        return _is_none_literal(node.args[0])
    for kw in node.keywords:
        if kw.arg == "executor":
            return _is_none_literal(kw.value)
    return False


def check_p031(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit P031 findings for offloads onto asyncio's shared default executor."""
    if filename.replace("\\", "/").endswith(_EXEMPT_SUFFIX):
        return []
    bindings = collect_import_bindings(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == _EXECUTOR_ATTR:
            if _executor_arg_is_none(node):
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id=RULE_ID,
                        node=node,
                        message=(
                            f"'.{_EXECUTOR_ATTR}(None, ...)' offloads onto the "
                            f"shared default executor. {_HINT}"
                        ),
                        directives=directives,
                    )
                )
            continue
        target = resolve_call_target(node.func, bindings)
        if target in _TO_THREAD_EXACT:
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id=RULE_ID,
                    node=node,
                    message=f"'{target}()' offloads onto the shared default executor. {_HINT}",
                    directives=directives,
                )
            )
    return findings
