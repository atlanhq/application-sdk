"""Entrypoint-conformance rule definitions (P017–P018, BLDX-1411).

Apps must boot through the SDK's prescribed entrypoint model rather than
constructing workers, clients, or HTTP servers themselves.  The SDK launcher
owns process startup — production invokes the base-image CLI
``application-sdk --mode {worker|handler|combined} --app module:ClassName``;
local dev uses ``application_sdk.main.run_dev_combined``.  Workers and
activities are auto-discovered from ``AppRegistry``/``TaskRegistry``; the app
never wires them.

These are P-series (prescription) rules, but they live in their own module and
are backed by a **separate** ``suite.checks.entrypoint`` check registration
whose discovery **scans test files too** — an integration harness that
hand-rolls a worker instead of using ``run_dev_combined`` is exactly the drift
to catch.  Multiple check modules under one series letter is the established
pattern (the orchestration-seam rules P004–P007 use the same split).

Scope
-----
Both rules are **app**-scoped: the SDK's ``main.py`` legitimately calls
``create_worker`` and ``uvicorn.run`` — that is its job.  These rules are only
meaningful on consumer apps, which must delegate startup to the SDK launcher.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P017",
        scope=RuleScope.APP,
        name="ManualWorkerBootstrap",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="entrypoint-conformance",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
        rationale=(
            "The SDK launcher owns worker startup: the base-image CLI "
            "(``application-sdk --mode worker|combined``) or "
            "``run_dev_combined`` in local dev auto-discovers every registered "
            "``App`` and ``task`` — the app never wires workers itself. Calling "
            "``create_worker``, ``create_temporal_client``, or ``AppWorker`` "
            "directly, importing removed v2 boot surface, or calling v2 "
            "lifecycle methods couples the app to infrastructure details that "
            "the SDK manages on its behalf, and breaks the guarantee that every "
            "app starts up in a consistent, SDK-controlled way (BLDX-1411)."
        ),
        short_description="App manually constructs a Temporal worker or client instead of using the SDK launcher",
        full_description=(
            "The app calls ``create_worker(...)``, ``create_temporal_client(...)``,\n"
            "or ``AppWorker(...)`` directly, imports removed v2 worker/client boot\n"
            "surface (``application_sdk.worker``, ``application_sdk.application``,\n"
            "``application_sdk.clients.temporal``), or calls a distinctive v2\n"
            "lifecycle method (``setup_workflow``, ``start_workflow``,\n"
            "``start_worker``).\n"
            "\n"
            "The SDK launcher owns worker startup entirely — subclass ``App``,\n"
            "define ``@task`` methods and a ``run()`` / ``@entrypoint`` method\n"
            "(typed ``Input``/``Output``), and set ``ATLAN_APP_MODULE=module:ClassName``.\n"
            "Production launches via the base-image CLI\n"
            "(``application-sdk --mode worker|combined --app ...``); local dev\n"
            "uses ``await run_dev_combined(MyApp, credentials={...}, ...)``.\n"
            "Workers and activities are auto-discovered from ``AppRegistry`` /\n"
            "``TaskRegistry`` — there is nothing to wire.\n"
            "\n"
            "Files under ``tests/integration/`` are exempt from the\n"
            "construction-call and lifecycle-call violation classes: those\n"
            "harnesses need an in-process worker/client handle to submit\n"
            "workflows and tear down per test, and ``run_dev_combined`` blocks\n"
            "until shutdown with no handle-returning mode to substitute. The\n"
            "removed-v2-import class stays enforced everywhere, including under\n"
            "``tests/integration/``.\n"
            "\n"
            "Land as ``WARN``: a justified inline\n"
            "``# conformance: ignore[P017] <reason>`` records any unavoidable\n"
            "exception outside that exemption and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p017",
    ),
    RuleDefinition(
        id="P018",
        scope=RuleScope.APP,
        name="ManualServerBootstrap",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="entrypoint-conformance",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
        rationale=(
            "The SDK owns the FastAPI handler server: ``run_handler_mode`` and "
            "``run_combined_mode`` create and manage the HTTP server on the "
            "app's behalf. Constructing ``FastAPI(...)`` directly, calling "
            "``uvicorn.run(...)`` manually, or invoking v2 server lifecycle "
            "methods (``setup_server``, ``start_server``, ``include_router``) "
            "couples the app to server-startup mechanics that the SDK controls, "
            "and prevents the SDK from evolving the handler server safely "
            "(BLDX-1411)."
        ),
        short_description="App manually constructs a FastAPI/uvicorn HTTP server instead of using the SDK launcher",
        full_description=(
            "The app constructs ``FastAPI(...)`` directly (name imported from\n"
            "``fastapi``), calls ``uvicorn.run(...)``, or invokes a distinctive\n"
            "v2 server lifecycle method (``setup_server``, ``start_server``,\n"
            "``include_router``).\n"
            "\n"
            "The SDK owns the FastAPI handler server: launch via the base-image\n"
            "CLI (``application-sdk --mode handler|combined --app ...``) or\n"
            "``run_dev_combined`` in local dev — the SDK creates and configures\n"
            "the FastAPI app and uvicorn server internally.  Express HTTP surface\n"
            "through ``@entrypoint`` methods on the ``App`` subclass; trigger\n"
            "them via ``POST /workflows/v1/start?entrypoint=<name>``.\n"
            "\n"
            "Land as ``WARN``: a justified inline\n"
            "``# conformance: ignore[P018] <reason>`` records any unavoidable\n"
            "exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p018",
    ),
)
