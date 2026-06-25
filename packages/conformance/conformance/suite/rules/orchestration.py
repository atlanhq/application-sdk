"""Orchestration-seam rule definitions (P004–P007, BLDX-1417).

Apps must interact with the orchestration layer **only through the SDK's
wrappers**, never the underlying Temporal engine, and the SDK must keep Temporal
contained behind that seam.  Keeping the SDK as the single seam lets the
orchestration layer evolve (Temporal upgrades, engine swaps, interceptor changes)
without breaking every app.

These are P-series (prescription) rules, but they live in their own module and are
backed by a **separate** ``suite.checks.orchestration`` check registration whose
discovery **scans test files too** — unlike the source-only ``suite.checks.prescriptions``
check behind P001–P003.  That is what lets an integration harness wiring Temporal
directly (e.g. ``tests/integration/conftest.py``) be caught.  Multiple check
modules under one series letter is the established pattern (the C-series uses
three).  P-ids are a permanent public contract (see ``prescriptions.py``).

Scope
-----
* ``P004`` / ``P005`` are **app**-scoped — they govern consumer apps, which must
  use the seam; they are skipped on the SDK, which publishes it.
* ``P006`` / ``P007`` are **sdk**-scoped — they govern the SDK's own containment
  of Temporal and are skipped on consumer apps.
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
        id="P004",
        scope=RuleScope.APP,
        name="DirectTemporalImport",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="orchestration-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.5.0",
        rationale=(
            "Apps must interact with the orchestration layer only through the SDK's "
            "wrappers, never the underlying Temporal engine. Keeping the SDK as the "
            "single seam lets the orchestration layer evolve (Temporal upgrades, "
            "engine swaps, interceptor changes) without breaking every app. A direct "
            "'temporalio' import couples the app to the engine and bypasses that seam "
            "(BLDX-1417)."
        ),
        short_description="App imports 'temporalio' directly instead of the SDK orchestration seam",
        full_description=(
            "A consumer app imports ``temporalio`` (the raw orchestration engine)\n"
            "directly.  Everything an app needs is re-exported through the SDK seam:\n"
            "runtime primitives and decorators via ``application_sdk.app``\n"
            "(``task``, ``signal``, ``query``, ``update``, ``now``, ``sleep``,\n"
            "``uuid4``, ``wait_condition``), and client/worker/converter construction\n"
            "via ``application_sdk.execution`` (``create_temporal_client``,\n"
            "``create_worker``, ``AppWorker``).  Importing ``temporalio`` directly\n"
            "couples the app to the engine and defeats the single-seam contract — see\n"
            "BLDX-1417.  This rule scans test/harness files too: an integration\n"
            "conftest that connects to Temporal directly is exactly the case to catch.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P004]\n"
            "<reason>`` records any unavoidable exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p004",
    ),
    RuleDefinition(
        id="P005",
        scope=RuleScope.APP,
        name="PrivateOrchestrationInternalImport",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="orchestration-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.5.0",
        rationale=(
            "The SDK's public orchestration seam is ``application_sdk.app`` and "
            "``application_sdk.execution``. Importing past it into private internals "
            "(``application_sdk.execution._temporal.*`` and other ``_``-prefixed "
            "modules) couples the app to implementation details that change without a "
            "deprecation cycle, so the SDK can no longer evolve the seam safely "
            "(BLDX-1417)."
        ),
        short_description="App imports SDK-private orchestration internals instead of the public seam",
        full_description=(
            "A consumer app imports from an SDK-private module — anything with a\n"
            "``_``-prefixed segment under ``application_sdk`` (most commonly\n"
            "``application_sdk.execution._temporal.*``) — or imports a private\n"
            "(``_``-prefixed) name from a public SDK module.  These internals are not\n"
            "a stable contract: the public re-exports in ``application_sdk.execution``\n"
            "(``create_temporal_client``, ``create_worker``, ``AppWorker``,\n"
            "``create_data_converter``) and ``application_sdk.app`` are the supported\n"
            "surface.  Where no public equivalent exists, raise it with the SDK team —\n"
            "see BLDX-1417.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P005]\n"
            "<reason>`` records any unavoidable exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p005",
    ),
    RuleDefinition(
        id="P006",
        scope=RuleScope.SDK,
        name="TemporalImportOutsideAdapter",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="orchestration-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.5.0",
        rationale=(
            "For the SDK to stay the single seam, Temporal must be contained behind "
            "one adapter (``application_sdk/execution/_temporal/``). When 'temporalio' "
            "is imported elsewhere in the SDK the engine bleeds across the codebase, "
            "and a future orchestration change has to be made in many places instead "
            "of one — the exact coupling this contract exists to prevent (BLDX-1417)."
        ),
        short_description="SDK imports 'temporalio' outside the execution/_temporal adapter",
        full_description=(
            "``temporalio`` is imported in an SDK module outside the orchestration\n"
            "adapter ``application_sdk/execution/_temporal/``.  The adapter is the one\n"
            "place that may touch the engine directly; the curated primitive\n"
            "re-export site ``application_sdk/app/__init__.py`` (the seam apps consume)\n"
            "and test files are exempt.  Everywhere else, relocate the Temporal usage\n"
            "into the adapter (or behind it) so the SDK keeps a single seam — see\n"
            "BLDX-1417.\n"
            "\n"
            "Land as ``WARN``: the current spread of in-SDK Temporal usage is the\n"
            "backlog this rule surfaces; some sites (e.g. workflow-class generation)\n"
            "are structural and stay until refactored.  Suppress a deliberate\n"
            "exception with ``# conformance: ignore[P006] <reason>``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p006",
    ),
    RuleDefinition(
        id="P007",
        scope=RuleScope.SDK,
        name="RawTemporalInPublicSurface",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="orchestration-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.5.0",
        rationale=(
            "If the SDK's public API hands callers raw Temporal objects (re-exports a "
            "'temporalio' symbol, or returns/accepts a 'temporalio' type), apps are "
            "coupled to the engine through the very surface meant to insulate them, "
            "and the seam leaks. The public contract must trade only in SDK-owned "
            "types so the engine can change underneath it (BLDX-1417)."
        ),
        short_description="SDK public API re-exports or exposes a raw 'temporalio' type",
        full_description=(
            "The SDK's public surface leaks Temporal in one of two ways: a public\n"
            "package ``__init__`` lists a name in ``__all__`` that was imported\n"
            "straight from ``temporalio``, or a publicly re-exported function exposes a\n"
            "raw ``temporalio`` type in a parameter/return annotation (e.g.\n"
            "``create_temporal_client() -> Client``, ``create_worker(client: Client)``).\n"
            "The curated runtime-primitive re-exports in ``application_sdk/app/__init__``\n"
            "are the sanctioned seam and are exempt, as are test files.  Closing a leak\n"
            "means wrapping the value in an opaque SDK type (a refactor) — see\n"
            "BLDX-1417.\n"
            "\n"
            "Land as ``WARN``: this rule detects the leaks; closing them is tracked\n"
            "separately.  Suppress a reviewed exception with\n"
            "``# conformance: ignore[P007] <reason>``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p007",
    ),
)
