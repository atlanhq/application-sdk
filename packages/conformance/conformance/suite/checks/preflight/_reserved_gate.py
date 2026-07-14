"""P032 ReservedPreflightActivityName + P033 DuplicateInWorkflowPreflight.

Both consume the same ``@task``-name pass. P032 fires on a task whose effective
activity name is exactly ``preflight`` (collides with the SDK-reserved gate
name). P033 fires on a task whose name carries a ``preflight`` token but is not
that exact name, and only when the app also defines a ``Handler.preflight_check``
(the two implementations drift). The two are mutually exclusive.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import (
    Registry,
    Source,
    _class_defs,
    _iter_class_body_methods,
    effective_task_name,
    find_preflight_check_sites,
    has_preflight_token,
    task_decorator,
)

_P032 = "P032"
_P033 = "P033"


def _handler_site_for(
    sites: list[tuple[Source, ast.AsyncFunctionDef]],
    src: Source,
    cls: ast.ClassDef,
) -> tuple[Source, ast.AsyncFunctionDef] | None:
    """Pick the preflight_check site to reference for a task in *cls*.

    Prefer a handler co-located in the same class, then the same source, and
    only then fall back to the first site — so the P033 message points at the
    implementation the offending task actually drifts from.
    """
    if not sites:
        return None
    class_methods = set(_iter_class_body_methods(cls))
    same_class = next((s for s in sites if s[0] is src and s[1] in class_methods), None)
    if same_class is not None:
        return same_class
    same_src = next((s for s in sites if s[0] is src), None)
    return same_src if same_src is not None else sites[0]


def scan(reg: Registry) -> list[Finding]:
    preflight_sites = find_preflight_check_sites(reg)

    findings: list[Finding] = []
    for src in reg.sources:
        for cls in _class_defs(src.tree):
            handler_site = _handler_site_for(preflight_sites, src, cls)
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
