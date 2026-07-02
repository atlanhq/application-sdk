"""P020 — NonDeterministicPrimitiveInWorkflow.

Flags wall-clock time, UUID, sleep, and randomness calls inside workflow-context
methods of an ``App`` subclass.  Workflow code is replayed deterministically, so
these must use the SDK seam (``self.now()`` / ``now``, ``self.uuid()`` / ``uuid4``,
``sleep``).  ``random`` / ``secrets`` are flagged too — they are real determinism
bugs — but have **no** SDK seam replacement, so their remediation is routed to
residue (raise a deterministic-random seam request) rather than an autofix.

Matching is receiver-anchored (see :func:`resolve_call_target`): only a ``.now()``
whose receiver resolves to ``datetime`` is flagged, so the sanctioned ``self.now()``
and ``application_sdk.app.now`` are left untouched.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.orchestration._temporal_common import (
    collect_import_bindings,
)
from conformance.suite.schema.findings import Finding

from ._workflow_methods import resolve_call_target, workflow_method_nodes

RULE_ID = "P020"

_TIME_TARGETS = frozenset(
    {
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "time.perf_counter_ns",
        "time.process_time",
        "time.process_time_ns",
    }
)
# uuid3/uuid5 are namespace-deterministic, so only the time/host (uuid1) and
# random (uuid4) variants are non-deterministic.
_UUID_TARGETS = frozenset({"uuid.uuid1", "uuid.uuid4"})
_SLEEP_TARGETS = frozenset({"time.sleep", "asyncio.sleep"})
_RANDOM_PREFIXES = ("random.", "secrets.")
_RANDOM_EXACT = frozenset({"os.urandom"})

_TIME_HINT = (
    "Workflow code is replayed and must be deterministic: use self.now() or "
    "'from application_sdk.app import now' instead."
)
_UUID_HINT = (
    "Workflow code is replayed and must be deterministic: use self.uuid() or "
    "'from application_sdk.app import uuid4' instead."
)
_SLEEP_HINT = (
    "Use 'from application_sdk.app import sleep' — a raw time.sleep()/asyncio.sleep() "
    "is non-deterministic under workflow replay."
)
_RANDOM_HINT = (
    "Randomness is non-deterministic under workflow replay and has no SDK seam "
    "replacement yet; move it into a @task or raise a deterministic-random seam "
    "request with the SDK team."
)


def _classify(target: str) -> str | None:
    """Return a remediation hint for *target* if it is a banned primitive, else None."""
    if target in _TIME_TARGETS:
        return _TIME_HINT
    if target in _UUID_TARGETS:
        return _UUID_HINT
    if target in _SLEEP_TARGETS:
        return _SLEEP_HINT
    if target in _RANDOM_EXACT or any(target.startswith(p) for p in _RANDOM_PREFIXES):
        return _RANDOM_HINT
    return None


def check_p020(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit P020 findings for non-deterministic primitives in workflow context."""
    methods = workflow_method_nodes(tree)
    if not methods:
        return []
    bindings = collect_import_bindings(tree)
    findings: list[Finding] = []
    for method in methods:
        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue
            target = resolve_call_target(node.func, bindings)
            if target is None:
                continue
            hint = _classify(target)
            if hint is None:
                continue
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id=RULE_ID,
                    node=node,
                    message=(
                        f"Non-deterministic call '{target}()' in workflow context. {hint}"
                    ),
                    directives=directives,
                )
            )
    return findings
