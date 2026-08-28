"""P047 PreflightFailureLoggedAsWarning (FND-901).

Flags ``logger.warning(...)`` / ``logger.warn(...)`` calls inside a
``Handler.preflight_check`` override. The customer-facing log view filters at
ERROR, so a preflight failure a handler logs at WARNING is invisible on exactly
the runs where the customer needs it. Failures belong in the typed check result
— the gate emits the one outcome row and levels it — and non-failure progress
belongs at INFO/DEBUG. Only class-method overrides are scanned (module-level
per-entrypoint ``preflight_check`` functions are not resolved by
``find_preflight_check_sites``), and helpers the method calls are not followed
— matching the false-negative-over-false-positive stance of the P series.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry, find_preflight_check_sites

_P047 = "P047"

_WARNING_METHODS = frozenset({"warning", "warn"})


def _is_logger_warning(node: ast.Call) -> bool:
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in _WARNING_METHODS):
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name):
        name = receiver.id
    elif isinstance(receiver, ast.Attribute):
        name = receiver.attr
    else:
        return False
    lowered = name.lower()
    return (
        lowered in {"logging", "log"}
        or lowered.endswith("logger")
        or lowered.endswith("_log")
    )


def scan(reg: Registry) -> list[Finding]:
    findings: list[Finding] = []
    for src, method in find_preflight_check_sites(reg):
        for node in ast.walk(method):
            if not (isinstance(node, ast.Call) and _is_logger_warning(node)):
                continue
            findings.append(
                make_finding(
                    filename=src.rel,
                    rule_id=_P047,
                    node=node,
                    message=(
                        "logger.warning() inside preflight_check — a preflight "
                        "failure logged at WARNING is invisible under the "
                        "customer's default ERROR log filter (FND-901). Return "
                        "the failure through the typed check result "
                        "(PreflightCheck(passed=False, error=...)) and let the "
                        "gate log the outcome at the right level; use INFO/DEBUG "
                        "for non-failure progress."
                    ),
                    directives=src.directives,
                )
            )
    return findings
