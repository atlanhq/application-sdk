"""P021 — SideEffectIoInWorkflow.

Flags a curated, high-signal set of side-effecting I/O calls inside workflow-context
methods of an ``App`` subclass: file access, network calls, environment reads, and
spawning threads/processes.  Workflow code is replayed deterministically and must
not touch the outside world — I/O belongs in a ``@task`` activity.

Remediation is structural ("move it into a @task"), so findings are routed to
residue rather than auto-fixed.  The list is a deliberate high-signal subset, not
an exhaustive enumeration of every I/O API.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.orchestration._temporal_common import (
    collect_import_bindings,
)
from conformance.suite.schema.findings import Finding

from ._workflow_methods import resolve_call_target, workflow_method_nodes

RULE_ID = "P021"

# Module-prefixed I/O surfaces: any call under these roots is side-effecting.
_IO_PREFIXES = (
    "requests.",
    "httpx.",
    "urllib.request.",
    "http.client.",
    "socket.",
    "subprocess.",
    "threading.",
    "multiprocessing.",
    "concurrent.futures.",
)
# Exact callables (builtins / specific functions) that perform I/O or env reads.
_IO_EXACT = frozenset(
    {
        "open",
        "os.getenv",
        "os.system",
        "os.popen",
        "os.environ.get",
    }
)
_ENV_OBJECT = "os.environ"

_HINT = (
    "Move it into a @task method — workflow code is replayed and must be "
    "deterministic, so file/network/env I/O belongs in an activity."
)


def _is_io_target(target: str) -> bool:
    return target in _IO_EXACT or any(target.startswith(p) for p in _IO_PREFIXES)


def check_p021(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit P021 findings for side-effecting I/O in workflow context."""
    methods = workflow_method_nodes(tree)
    if not methods:
        return []
    bindings = collect_import_bindings(tree)
    findings: list[Finding] = []
    for method in methods:
        for node in ast.walk(method):
            target: str | None = None
            label: str | None = None
            if isinstance(node, ast.Call):
                target = resolve_call_target(node.func, bindings)
                if target is not None and _is_io_target(target):
                    label = f"{target}()"
            elif isinstance(node, ast.Subscript):
                # os.environ["X"] read.
                if resolve_call_target(node.value, bindings) == _ENV_OBJECT:
                    target = _ENV_OBJECT
                    label = f"{_ENV_OBJECT}[...]"
            if label is None:
                continue
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id=RULE_ID,
                    node=node,
                    message=f"Side-effecting I/O '{label}' in workflow context. {_HINT}",
                    directives=directives,
                )
            )
    return findings
