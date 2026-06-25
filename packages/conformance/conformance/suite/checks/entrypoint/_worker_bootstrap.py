"""P016 ManualWorkerBootstrap — ban hand-rolling a Temporal worker or client.

The SDK launcher owns worker startup: ``application-sdk --mode worker|combined``
(production) or ``run_dev_combined`` (local dev) auto-discovers every registered
``App`` and ``task`` — the app never wires workers itself.

Three violation classes are flagged:

1. **Worker construction calls** — a call to ``create_worker(...)``,
   ``create_temporal_client(...)``, or ``AppWorker(...)`` whose binding resolves
   to ``application_sdk.execution``.  Importing those symbols is fine (P004/P005
   sanction the public seam); *calling* the constructor is the boot path the SDK
   launcher owns.

2. **Legacy v2 boot imports** — any import from the removed v2 surface:
   ``application_sdk.worker``, ``application_sdk.application`` (and submodules),
   ``application_sdk.clients.temporal``.  These modules were deleted in v3; their
   presence signals an unmigrated boot path.

3. **Lifecycle method calls** — a call to ``.setup_workflow(...)``,
   ``.start_workflow(...)``, or ``.start_worker(...)`` on any object.  These are
   distinctive enough to be reliable signals even without type information.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._bootstrap_common import collect_import_origins

# ── Worker-construction symbols (from application_sdk.execution) ─────────────

# Fully-qualified origins that signal manual worker/client construction.
# create_data_converter is NOT included — it configures serialisation, not boot.
_WORKER_CONSTRUCTION_ORIGINS: frozenset[str] = frozenset(
    {
        "application_sdk.execution.create_worker",
        "application_sdk.execution.create_temporal_client",
        "application_sdk.execution.AppWorker",
    }
)

# ── Removed v2 boot surface ───────────────────────────────────────────────────

# Module prefixes whose presence in any import signals the legacy v2 boot path.
# These modules were removed in v3 — any import from them is a migration signal.
_V2_BOOT_MODULE_PREFIXES: tuple[str, ...] = (
    "application_sdk.worker",
    "application_sdk.application",
    "application_sdk.clients.temporal",
)

# ── v2 lifecycle method names ─────────────────────────────────────────────────

_WORKER_LIFECYCLE_METHODS: frozenset[str] = frozenset(
    {
        "setup_workflow",
        "start_workflow",
        "start_worker",
    }
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_v2_boot_module(module: str) -> bool:
    """True if *module* is or is a submodule of a removed v2 boot module."""
    return any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in _V2_BOOT_MODULE_PREFIXES
    )


# ── Check ─────────────────────────────────────────────────────────────────────


def check_p016(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P016 findings for manual worker/client bootstrap patterns."""
    findings: list[Finding] = []
    origins = collect_import_origins(tree)

    for node in ast.walk(tree):
        # (a) Legacy v2 boot imports
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if _is_v2_boot_module(module):
                imported = ", ".join((a.asname or a.name) for a in node.names)
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P016",
                        node=node,
                        message=(
                            f"Imports removed v2 boot surface "
                            f"('from {module} import {imported}') — these "
                            "modules were deleted in v3. Subclass "
                            "'application_sdk.app.App' and launch via "
                            "'application-sdk --mode worker|combined' (prod) "
                            "or 'run_dev_combined(MyApp, ...)' (dev); workers "
                            "are auto-discovered, nothing to wire. See "
                            "BLDX-1411. Suppress with "
                            "'# conformance: ignore[P016] <reason>'."
                        ),
                        directives=directives,
                    )
                )

        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_v2_boot_module(alias.name):
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id="P016",
                            node=node,
                            message=(
                                f"Imports removed v2 boot module "
                                f"('{alias.name}') — this module was deleted "
                                "in v3. Subclass 'application_sdk.app.App' "
                                "and launch via "
                                "'application-sdk --mode worker|combined' "
                                "(prod) or 'run_dev_combined(MyApp, ...)' "
                                "(dev). See BLDX-1411. Suppress with "
                                "'# conformance: ignore[P016] <reason>'."
                            ),
                            directives=directives,
                        )
                    )

        elif isinstance(node, ast.Call):
            func = node.func

            # (b) Worker construction calls via direct name (e.g. create_worker(...))
            if isinstance(func, ast.Name):
                origin = origins.get(func.id, "")
                if origin in _WORKER_CONSTRUCTION_ORIGINS:
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id="P016",
                            node=node,
                            message=(
                                f"Calls '{func.id}(...)' directly — worker "
                                "and client construction is the SDK launcher's "
                                "job, not the app's. Launch via "
                                "'application-sdk --mode worker|combined' "
                                "(prod) or 'run_dev_combined(MyApp, ...)' "
                                "(dev); workers are auto-discovered from "
                                "'AppRegistry'/'TaskRegistry'. See BLDX-1411. "
                                "Suppress with "
                                "'# conformance: ignore[P016] <reason>'."
                            ),
                            directives=directives,
                        )
                    )

            # (c) Lifecycle method calls (any object receiver)
            elif isinstance(func, ast.Attribute):
                if func.attr in _WORKER_LIFECYCLE_METHODS:
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id="P016",
                            node=node,
                            message=(
                                f"Calls '.{func.attr}(...)' — this is a v2 "
                                "worker-lifecycle method. In v3 the SDK "
                                "launcher manages the full boot sequence; "
                                "subclass 'App' and use "
                                "'run_dev_combined'/'application-sdk'. See "
                                "BLDX-1411. Suppress with "
                                "'# conformance: ignore[P016] <reason>'."
                            ),
                            directives=directives,
                        )
                    )

    return findings
