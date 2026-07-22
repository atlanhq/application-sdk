"""P036 — HandRolledProcessIsolation.

Flags direct construction of a process-based execution primitive —
``ProcessPoolExecutor`` or ``multiprocessing.Process`` / ``Pool`` — instead of
routing crash-prone or best-effort native work through the SDK's sanctioned
child-process seam, ``run_fault_isolated`` / ``run_best_effort``
(``application_sdk.execution.heartbeat``).

Why this matters: a native fault (a ``SIGSEGV`` in a C extension) is not a Python
exception — it bypasses every ``try/except`` and, in a worker thread, kills the
whole Temporal worker mid-poll. ``run_fault_isolated`` runs the work in an
isolated child process so the fault is contained and surfaces as a catchable
``BrokenProcessPool``; ``run_best_effort`` layers warn-and-continue on top for
non-essential work whose result may be safely skipped. Hand-rolling a
``ProcessPoolExecutor`` / ``multiprocessing`` child re-implements that seam
without its crash containment, timeout, spawn-not-fork safety, and width
management, and fragments the worker's process-management model.

Deliberately narrow to stay false-positive-free: only the *construction* of these
primitives is flagged, resolved through imports/aliases (so
``from concurrent.futures import ProcessPoolExecutor as PPE; PPE(...)`` is caught).
``multiprocessing.get_context(...).Process()`` — whose receiver is a runtime
object — is not statically resolvable and is not flagged. ``ThreadPoolExecutor``
is not a process and is out of scope (thread offload is governed by P031).

``execution/heartbeat.py`` is exempt: that is where the sanctioned seam's own
``ProcessPoolExecutor`` lives.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.orchestration._temporal_common import (
    collect_import_bindings,
)
from conformance.suite.schema.findings import Finding

from ._workflow_methods import resolve_call_target

RULE_ID = "P036"

# Dotted targets that construct a Python child-process execution primitive.
# Matched via import-binding resolution, so aliases and both `import x` and
# `from x import y` forms resolve to the same canonical name.
_PROCESS_PRIMITIVES = frozenset(
    {
        "concurrent.futures.ProcessPoolExecutor",
        "concurrent.futures.process.ProcessPoolExecutor",
        "multiprocessing.Process",
        "multiprocessing.Pool",
        "multiprocessing.pool.Pool",
    }
)

# Callable *final names* distinctive enough to match on their own, regardless of
# receiver. ``ProcessPoolExecutor`` is unique enough to have no false positives,
# and matching by name catches the ``import concurrent.futures;
# concurrent.futures.ProcessPoolExecutor()`` form that import-binding resolution
# mangles (the collector maps ``concurrent`` -> ``concurrent.futures``). Bare
# ``Process`` / ``Pool`` are deliberately *not* here — they are common attribute
# names on unrelated objects — so those stay import-resolved only.
_PROCESS_PRIMITIVE_NAMES = frozenset({"ProcessPoolExecutor"})


def _callable_final_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


# The one file allowed to construct a process pool directly: this is where the
# sanctioned run_fault_isolated() / run_best_effort() seam lives.
_EXEMPT_SUFFIX = "execution/heartbeat.py"

_HINT = (
    "This hand-rolls process isolation. A native fault (segfault in a C "
    "extension) in a worker thread kills the whole Temporal worker; route "
    "crash-prone or best-effort native work through the SDK's sanctioned "
    "child-process seam instead — run_fault_isolated() (raises a catchable "
    "BrokenProcessPool) or run_best_effort() (warn-and-continue) from "
    "application_sdk.execution.heartbeat."
)


def check_p036(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit P036 findings for hand-rolled process-isolation primitives."""
    if filename.replace("\\", "/").endswith(_EXEMPT_SUFFIX):
        return []
    bindings = collect_import_bindings(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = resolve_call_target(node.func, bindings)
        matched_by_target = target in _PROCESS_PRIMITIVES
        final_name = _callable_final_name(node.func)
        if matched_by_target or final_name in _PROCESS_PRIMITIVE_NAMES:
            display = target if matched_by_target else final_name
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id=RULE_ID,
                    node=node,
                    message=f"'{display}(...)' hand-rolls process isolation. {_HINT}",
                    directives=directives,
                )
            )
    return findings
