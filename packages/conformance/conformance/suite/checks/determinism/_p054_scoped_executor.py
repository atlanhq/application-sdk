"""P054 — ScopedExecutorJoinedOnCancel.

Flags ``with ThreadPoolExecutor() as pool:`` inside an ``async def`` whose body
offloads to that same executor with ``.run_in_executor(pool, ...)``.

Why this matters: exit from the ``with`` block calls ``pool.shutdown(wait=True)``
on the event loop thread.  If the awaiting task is cancelled while the executor is
running a blocking driver call, that ``wait=True`` blocks the loop until the call
returns and freezes the entire worker.  A dedicated executor created without
``with`` and shut down with ``executor.shutdown(wait=False)`` in ``finally`` lets
the cancel return immediately while the call finishes unjoined; that shape is
correct with or without thread affinity (FND-2873).  For a single offload call
with no thread affinity, ``run_in_thread(fn, ...)`` is the SDK seam and the
simpler fix.

Matching is construction-anchored and import-resolved, so an aliased
``from concurrent.futures import ThreadPoolExecutor as TPE`` is caught.

Out of scope: ``run_in_executor(None, ...)`` (P031), a pool built earlier and used
as ``with pool:``, ``asyncio.wrap_future(pool.submit(...))``, sync ``def``, and an
executor the ``with`` does not itself bind.  Nested ``def`` / ``async def`` /
``lambda`` bodies are not descended into.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.orchestration._temporal_common import (
    collect_import_bindings,
)
from conformance.suite.schema.findings import Finding

from ._workflow_methods import resolve_call_target

RULE_ID = "P054"

# Dotted targets that construct a thread pool, matched via import-binding
# resolution so aliases and both `import x` and `from x import y` forms resolve
# to the same canonical name.
_THREAD_POOLS = frozenset(
    {
        "concurrent.futures.ThreadPoolExecutor",
        "concurrent.futures.thread.ThreadPoolExecutor",
    }
)

# A final name distinctive enough to match on its own. Needed because the
# import-binding collector resolves `import concurrent.futures;
# concurrent.futures.ThreadPoolExecutor()` to a wrong dotted name; same fallback
# as P036.
_THREAD_POOL_NAMES = frozenset({"ThreadPoolExecutor"})

_EXECUTOR_ATTR = "run_in_executor"

_NESTED_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

_HINT = (
    "A `with ThreadPoolExecutor() as pool:` block exits through "
    "pool.shutdown(wait=True) on the event loop thread, so cancelling the awaiting "
    "task blocks the whole worker until the blocking call returns. Keep a "
    "dedicated executor created without `with` and call "
    "executor.shutdown(wait=False) in `finally`, or, for a single call with no "
    "thread affinity, use run_in_thread(fn, ...)."
)


def _callable_final_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _binds_thread_pool(item: ast.withitem, bindings: dict[str, str]) -> str | None:
    """Return the name bound by ``with <ThreadPoolExecutor>() as name``."""
    ctx = item.context_expr
    if not isinstance(ctx, ast.Call):
        return None
    target = resolve_call_target(ctx.func, bindings)
    if (
        target not in _THREAD_POOLS
        and _callable_final_name(ctx.func) not in _THREAD_POOL_NAMES
    ):
        return None
    bound = item.optional_vars
    if isinstance(bound, ast.Name):
        return bound.id
    return None


def _executor_is_name(node: ast.Call, name: str) -> bool:
    """True if *node*'s executor argument — first positional or ``executor=`` — is *name*."""
    if node.args:
        first = node.args[0]
        return isinstance(first, ast.Name) and first.id == name
    for kw in node.keywords:
        if kw.arg == "executor":
            return isinstance(kw.value, ast.Name) and kw.value.id == name
    return False


def _iter_no_nested_defs(nodes: Iterable[ast.AST]) -> Iterator[ast.AST]:
    for node in nodes:
        yield node
        if isinstance(node, _NESTED_DEFS):
            continue
        yield from _iter_no_nested_defs(ast.iter_child_nodes(node))


def _offloads_to(node: ast.With, name: str) -> bool:
    for inner in _iter_no_nested_defs(node.body):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == _EXECUTOR_ATTR
            and _executor_is_name(inner, name)
        ):
            return True
    return False


def check_p054(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit P054 findings for a ``with``-scoped ThreadPoolExecutor joined on cancel."""
    bindings = collect_import_bindings(tree)
    findings: list[Finding] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in _iter_no_nested_defs(fn.body):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                name = _binds_thread_pool(item, bindings)
                if name is not None and _offloads_to(node, name):
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id=RULE_ID,
                            node=node,
                            message=(
                                f"'with ... as {name}:' scopes a ThreadPoolExecutor "
                                f"that is joined on cancel. {_HINT}"
                            ),
                            directives=directives,
                        )
                    )
                    break
    return findings
