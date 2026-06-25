"""P018 ManualServerBootstrap — ban hand-rolling a FastAPI/uvicorn HTTP server.

The SDK owns the FastAPI handler server: ``run_handler_mode`` and
``run_combined_mode`` create and manage it on the app's behalf.  Apps express
HTTP surface through ``@entrypoint`` methods and trigger them via
``POST /workflows/v1/start?entrypoint=<name>``.

Three violation classes are flagged:

1. **FastAPI/uvicorn construction** — a ``FastAPI(...)`` call whose binding
   resolves to ``fastapi.FastAPI`` or ``fastapi.applications.FastAPI``
   (including dotted calls like ``fastapi.FastAPI(...)`` after
   ``import fastapi``); a ``uvicorn.Server(...)`` or ``uvicorn.Config(...)``
   call; or a ``uvicorn.run(...)`` call (either via dotted attribute access
   after ``import uvicorn``, or as a bare name after
   ``from uvicorn import run``).  The SDK creates and manages all of these
   internally via the launcher.

2. **Lifecycle method calls** — a call to ``.setup_server(...)``,
   ``.start_server(...)``, or ``.include_router(...)`` on a
   plausibly-Atlan-App receiver (``self``, ``app``, or a name imported from
   ``application_sdk``).  Receiver restriction avoids false positives on
   ``asyncio.start_server(...)`` (stdlib) and routers that attach sub-routers
   (``router.include_router(sub)``).
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._bootstrap_common import (
    collect_import_origins,
    is_app_receiver,
    qualify_chained_attr_call,
)

# ── FastAPI construction origins ──────────────────────────────────────────────

# fastapi.applications is FastAPI's actual module; fastapi/__init__.py re-exports
# FastAPI from there, so both ``from fastapi import FastAPI`` and
# ``from fastapi.applications import FastAPI`` must be caught.
_FASTAPI_CONSTRUCTION_ORIGINS: frozenset[str] = frozenset(
    {
        "fastapi.FastAPI",
        "fastapi.applications.FastAPI",
    }
)

# ── uvicorn origins ───────────────────────────────────────────────────────────

# Bare ``uvicorn.run(...)`` and ``uvicorn.Server(...)`` / ``uvicorn.Config(...)``
# all signal manual server construction the SDK launcher owns.
_UVICORN_RUN_ORIGINS: frozenset[str] = frozenset({"uvicorn.run"})
_UVICORN_MODULE_ORIGIN = "uvicorn"
_UVICORN_SERVER_CONSTRUCTION_ORIGINS: frozenset[str] = frozenset(
    {
        "uvicorn.Server",
        "uvicorn.Config",
    }
)

# ── Lifecycle method names ────────────────────────────────────────────────────

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

            # (a) FastAPI(...) where FastAPI was imported from fastapi[.applications]
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

            # (a cont'd) uvicorn.Server(...) / uvicorn.Config(...)
            elif origin in _UVICORN_SERVER_CONSTRUCTION_ORIGINS:
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P018",
                        node=node,
                        message=(
                            f"Constructs '{func.id}(...)' directly — the SDK "
                            "manages the uvicorn server internally via the "
                            "launcher. Launch via 'application-sdk --mode "
                            "handler|combined' (prod) or "
                            "'run_dev_combined(MyApp, ...)' (dev). See "
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
            if isinstance(func.value, ast.Name):
                module_origin = origins.get(func.value.id, "")
                dotted = f"{module_origin}.{func.attr}"

                # (a) fastapi.FastAPI(...) after `import fastapi`
                if dotted in _FASTAPI_CONSTRUCTION_ORIGINS:
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

                # (a cont'd) uvicorn.Server(...) / uvicorn.Config(...) via module attr
                elif dotted in _UVICORN_SERVER_CONSTRUCTION_ORIGINS:
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id="P018",
                            node=node,
                            message=(
                                f"Constructs '{func.value.id}.{func.attr}(...)' "
                                "directly — the SDK manages the uvicorn server "
                                "internally via the launcher. Launch via "
                                "'application-sdk --mode handler|combined' (prod) "
                                "or 'run_dev_combined(MyApp, ...)' (dev). See "
                                "BLDX-1411. Suppress with "
                                "'# conformance: ignore[P018] <reason>'."
                            ),
                            directives=directives,
                        )
                    )

                # (b) uvicorn.run(...) via dotted attribute access
                elif func.attr == "run" and module_origin == _UVICORN_MODULE_ORIGIN:
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

                # (c) Server lifecycle method calls on app-receiver only
                elif func.attr in _SERVER_LIFECYCLE_METHODS and is_app_receiver(
                    func.value.id, module_origin
                ):
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id="P018",
                            node=node,
                            message=(
                                f"Calls '.{func.attr}(...)' — manual server "
                                "lifecycle call that the SDK manages. In v3 the "
                                "SDK owns the FastAPI server and the app has no "
                                "reference to it; express HTTP surface through "
                                "'@entrypoint' and launch via 'application-sdk'. "
                                "See BLDX-1411. Suppress with "
                                "'# conformance: ignore[P018] <reason>'."
                            ),
                            directives=directives,
                        )
                    )

            else:
                # (a cont'd) chained dotted construction:
                # `import fastapi.applications` then
                # `fastapi.applications.FastAPI(...)`
                qualified = qualify_chained_attr_call(func, origins)
                if qualified is not None:
                    if qualified in _FASTAPI_CONSTRUCTION_ORIGINS:
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
                    elif qualified in _UVICORN_SERVER_CONSTRUCTION_ORIGINS:
                        findings.append(
                            make_finding(
                                filename=filename,
                                rule_id="P018",
                                node=node,
                                message=(
                                    f"Constructs '{qualified}(...)' "
                                    "directly — the SDK manages the uvicorn server "
                                    "internally via the launcher. Launch via "
                                    "'application-sdk --mode handler|combined' (prod) "
                                    "or 'run_dev_combined(MyApp, ...)' (dev). See "
                                    "BLDX-1411. Suppress with "
                                    "'# conformance: ignore[P018] <reason>'."
                                ),
                                directives=directives,
                            )
                        )

    return findings
