"""P032 ReservedPreflightActivityName + P033 DuplicateInWorkflowPreflight.

Both consume the same ``@task``-name pass. P032 fires on a task whose effective
activity name is exactly ``preflight`` (collides with the SDK-reserved gate
name). P033 fires on a task whose name carries a ``preflight`` token but is not
that exact name, and only when the app also defines a ``Handler.preflight_check``
(the two implementations drift). The two are mutually exclusive.
"""

from __future__ import annotations

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import (
    Registry,
    _class_defs,
    _iter_class_body_methods,
    effective_task_name,
    find_preflight_check_sites,
    has_preflight_token,
    task_decorator,
)

_P032 = "P032"
_P033 = "P033"


def scan(reg: Registry) -> list[Finding]:
    preflight_sites = find_preflight_check_sites(reg)
    handler_site = preflight_sites[0] if preflight_sites else None

    findings: list[Finding] = []
    for src in reg.sources:
        for cls in _class_defs(src.tree):
            for func in _iter_class_body_methods(cls):
                dec = task_decorator(func, src.prov)
                if dec is None:
                    continue
                name = effective_task_name(func, dec)
                if name is None:
                    continue
                if name == "preflight":
                    findings.append(
                        make_finding(
                            filename=src.rel,
                            rule_id=_P032,
                            node=func,
                            message=(
                                "@task registers the activity name 'preflight', which "
                                "collides with the SDK-reserved preflight-gate activity "
                                "'{app_name}:preflight'; the worker fails to boot with "
                                "WorkerActivityNameCollisionError. Rename the task or "
                                "fold it into Handler.preflight_check."
                            ),
                            directives=src.directives,
                        )
                    )
                elif handler_site is not None and has_preflight_token(name):
                    findings.append(
                        make_finding(
                            filename=src.rel,
                            rule_id=_P033,
                            node=func,
                            message=(
                                f"@task '{name}' is a second preflight implementation "
                                f"alongside Handler.preflight_check ({handler_site[0].rel}:"
                                f"{handler_site[1].lineno}); the two drift. "
                                "The SDK gate calls preflight_check — remove the "
                                "app-owned preflight activity."
                            ),
                            directives=src.directives,
                        )
                    )
    return findings
