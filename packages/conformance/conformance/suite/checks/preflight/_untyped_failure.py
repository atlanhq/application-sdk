"""P034 UntypedPreflightCheckFailure.

Flags a ``PreflightCheck(...)`` constructed with an explicit ``passed=False`` and
no typed ``error=`` (absent, or the literal ``None``). Only the SDK
``PreflightCheck`` is matched — a locally-defined same-named class is ignored.
Requiring the explicit ``passed=False`` literal keeps false positives near zero:
bare ``PreflightCheck(name=...)`` templates and non-literal ``passed`` are left
alone.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry, is_preflightcheck_call, sdk_preflightcheck_locals

_P034 = "P034"
_MISSING = object()


def scan(reg: Registry) -> list[Finding]:
    findings: list[Finding] = []
    for src in reg.sources:
        local_names = sdk_preflightcheck_locals(src.tree)
        module_aliases = src.prov.sdk_contract_module_aliases
        if not local_names and not module_aliases:
            continue
        for node in ast.walk(src.tree):
            if not isinstance(node, ast.Call):
                continue
            if not is_preflightcheck_call(node.func, local_names, module_aliases):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
            passed = kwargs.get("passed")
            if not (isinstance(passed, ast.Constant) and passed.value is False):
                continue
            error = kwargs.get("error", _MISSING)
            error_typed = not (
                error is _MISSING
                or (isinstance(error, ast.Constant) and error.value is None)
            )
            if error_typed:
                continue
            findings.append(
                make_finding(
                    filename=src.rel,
                    rule_id=_P034,
                    node=node,
                    message=(
                        "PreflightCheck(passed=False) has no typed error=; the gate "
                        "falls back to the generic PREFLIGHT_CHECK_FAILED code and the "
                        "Automation Engine loses category/code/audience/suggested_action. "
                        "Pass error=<AppError>.to_failure_details()."
                    ),
                    directives=src.directives,
                )
            )
    return findings
