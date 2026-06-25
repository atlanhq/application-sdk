"""P018 ManualServerBootstrap — ban hand-rolling a FastAPI/uvicorn HTTP server.

The SDK owns the FastAPI handler server: ``run_handler_mode`` and
``run_combined_mode`` create and manage it on the app's behalf.  Apps express
HTTP surface through ``@entrypoint`` methods and trigger them via
``POST /workflows/v1/start?entrypoint=<name>``.

Three violation classes are flagged:

1. **FastAPI construction** — a ``FastAPI(...)`` call whose binding resolves to
   ``fastapi.FastAPI``.  The SDK creates the FastAPI instance internally; the
   app should never instantiate it directly.

2. **uvicorn.run()** — a ``uvicorn.run(...)`` call (either via dotted attribute
   access after ``import uvicorn``, or as a bare name after
   ``from uvicorn import run``).  The SDK invokes uvicorn internally via the
   launcher.

3. **Lifecycle method calls** — a call to ``.setup_server(...)``,
   ``.start_server(...)``, or ``.include_router(...)`` on any object.  These
   are the v2 server-wiring methods that no longer apply: the SDK owns the
   server and the app has no reference to it.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._bootstrap_common import collect_import_origins

# ── FastAPI and uvicorn origins ───────────────────────────────────────────────

# Fully-qualified origins that signal a manual FastAPI construction.
_FASTAPI_CONSTRUCTION_ORIGINS: frozenset[str] = frozenset({"fastapi.FastAPI"})

# Fully-qualified origins that signal a manual uvicorn call (run() from uvicorn
# as a bound name OR the top-level "uvicorn" package used as a callable target).
_UVICORN_RUN_ORIGINS: frozenset[str] = frozenset({"uvicorn.run"})
_UVICORN_MODULE_ORIGIN = "uvicorn"

# ── v2 server-lifecycle method names ─────────────────────────────────────────

_SERVER_LIFECYCLE_METHODS: frozenset[str] = frozenset(
    {
        "setup_server",
        "start_server",
        "include_router",
    }
)


# ── Check ─────────────────────────────────────────────────────────────────────


def check_p018(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P018 findings for manual server bootstrap patterns."""
    findings: list[Finding] = []
    origins = collect_import_origins(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func

        if isinstance(func, ast.Name):
            origin = origins.get(func.id, "")

            # (a) FastAPI(...) where FastAPI was imported from fastapi
            if origin in _FASTAPI_CONSTRUCTION_ORIGINS:
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P018",
                        node=node,
                        message=(
                            "Constructs 'FastAPI(...)' directly — the SDK "
                            "creates and manages the FastAPI instance inside "
                            "'run_handler_mode'/'run_combined_mode'. Launch "
                            "via 'application-sdk --mode handler|combined' "
                            "(prod) or 'run_dev_combined(MyApp, ...)' (dev); "
                            "express HTTP surface through '@entrypoint'. See "
                            "BLDX-1411. Suppress with "
                            "'# conformance: ignore[P018] <reason>'."
                        ),
                        directives=directives,
                    )
                )

            # (b) run(...) where run was imported from uvicorn
            elif origin in _UVICORN_RUN_ORIGINS:
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P018",
                        node=node,
                        message=(
                            "Calls 'uvicorn.run(...)' directly — the SDK "
                            "invokes uvicorn internally via the launcher. "
                            "Launch via 'application-sdk --mode "
                            "handler|combined' (prod) or "
                            "'run_dev_combined(MyApp, ...)' (dev). See "
                            "BLDX-1411. Suppress with "
                            "'# conformance: ignore[P018] <reason>'."
                        ),
                        directives=directives,
                    )
                )

        elif isinstance(func, ast.Attribute):
            # (b) uvicorn.run(...) via dotted attribute access
            if (
                func.attr == "run"
                and isinstance(func.value, ast.Name)
                and origins.get(func.value.id, "") == _UVICORN_MODULE_ORIGIN
            ):
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P018",
                        node=node,
                        message=(
                            "Calls 'uvicorn.run(...)' directly — the SDK "
                            "invokes uvicorn internally via the launcher. "
                            "Launch via 'application-sdk --mode "
                            "handler|combined' (prod) or "
                            "'run_dev_combined(MyApp, ...)' (dev). See "
                            "BLDX-1411. Suppress with "
                            "'# conformance: ignore[P018] <reason>'."
                        ),
                        directives=directives,
                    )
                )

            # (c) Server lifecycle method calls (any object receiver)
            elif func.attr in _SERVER_LIFECYCLE_METHODS:
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P018",
                        node=node,
                        message=(
                            f"Calls '.{func.attr}(...)' — this is a v2 "
                            "server-lifecycle method. In v3 the SDK owns the "
                            "FastAPI server and the app has no reference to "
                            "it; express HTTP surface through '@entrypoint' "
                            "and launch via 'application-sdk'. See BLDX-1411. "
                            "Suppress with "
                            "'# conformance: ignore[P018] <reason>'."
                        ),
                        directives=directives,
                    )
                )

    return findings
