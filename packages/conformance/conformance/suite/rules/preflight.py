"""Preflight-gate rule definitions (P-series, BLDX-1545).

The SDK injects a mandatory ``{app_name}:preflight`` activity as the first step
of every extraction workflow; it runs the app's ``Handler.preflight_check`` and
blocks the run on a ``NOT_READY`` verdict (application-sdk PRs #2361, #2626).
These rules make the gate pattern statically enforceable across the fleet:
they surface a boot-time collision at review time (P032), the app-owned duplicate
that drifts from the gate (P033), untyped failure results that lose their wire
metadata (P034), the silent metadata/contract drift that no runtime signal
can catch (P035), and preflight failures logged below the customer's default
ERROR filter (P047, FND-901).

The detector lives in ``suite.checks.preflight``; these rules reuse the ``P``
series so they run on the existing P leg of the fleet CI matrix with no workflow
change. Per the P-series stability policy (see ``prescriptions.py``) a P-id is a
permanent public contract and is never renumbered or reused.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

_HELP_BASE = (
    "https://github.com/atlanhq/application-sdk/blob/main/"
    "packages/conformance/conformance/docs/rules/prescriptions.md"
)

_EXISTING_RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P032",
        scope=RuleScope.APP,
        name="ReservedPreflightActivityName",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.15.0",
        rationale=(
            "The SDK injects a mandatory pre-extraction gate as the activity "
            "'{app_name}:preflight'. An app @task that also registers the 'preflight' "
            "activity name collides with it: the worker raises "
            "WorkerActivityNameCollisionError at boot and never starts. Catching the "
            "collision statically surfaces it in the PR instead of on the first deploy. "
            "Customer impact: the worker never comes up, so every workflow the customer "
            "runs on that app is down from the moment the release deploys into their "
            "tenant — a full-app outage caused by a name collision no test exercises "
            "and no build gate sees."
        ),
        short_description=(
            "An app @task registers the 'preflight' activity name reserved by the SDK gate"
        ),
        full_description=(
            "The SDK reserves the activity name ``{app_name}:preflight`` for the "
            "injected preflight gate and registers it unconditionally on the worker. "
            "An app ``@task`` whose effective activity name is ``preflight`` (an explicit "
            '``@task(name="preflight")`` or a bare ``@task`` on a method named '
            "``preflight``) collides with the reserved name; worker boot fails with "
            "``WorkerActivityNameCollisionError``.\n"
            "\n"
            "Remediation: rename the task, or fold its logic into the app's "
            "``Handler.preflight_check`` (which the gate already calls). A non-literal "
            "``@task(name=<expr>)`` is not statically resolvable and is not flagged."
        ),
        help_uri=f"{_HELP_BASE}#p032",
    ),
    RuleDefinition(
        id="P033",
        scope=RuleScope.APP,
        name="DuplicateInWorkflowPreflight",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.15.0",
        rationale=(
            "An app that defines Handler.preflight_check AND its own preflight-named "
            "@task has two preflight implementations that inevitably drift. The gate "
            "runs preflight_check, so the app-owned activity is redundant — the exact "
            "anti-pattern the SDK-native gate eliminates."
        ),
        short_description=(
            "App defines Handler.preflight_check and a separate preflight-named @task that will drift"
        ),
        full_description=(
            "When an app declares a ``Handler.preflight_check`` and also registers its "
            "own preflight-named ``@task`` (any ``@task`` whose effective name contains "
            "``preflight`` as a token but is not the reserved gate name — that exact "
            "case is P032), the two preflight paths diverge over time. The SDK gate "
            "invokes ``Handler.preflight_check``; the app-owned activity is dead weight "
            "that silently rots.\n"
            "\n"
            "Remediation: delete the app-owned preflight activity and keep the single "
            "``Handler.preflight_check`` implementation the gate calls."
        ),
        help_uri=f"{_HELP_BASE}#p033",
    ),
    RuleDefinition(
        id="P034",
        scope=RuleScope.APP,
        name="UntypedPreflightCheckFailure",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.15.0",
        rationale=(
            "A failed PreflightCheck constructed without a typed error= falls back to "
            "the generic PREFLIGHT_CHECK_FAILED code, so the Automation Engine and the "
            "UI lose the category/code/audience/suggested_action the typed form carries "
            "on the wire. Customer impact: failed workflows lose actionable typed failure details."
        ),
        short_description=(
            "PreflightCheck(passed=False) constructed without a typed error= — untyped failure"
        ),
        full_description=(
            "A ``PreflightCheck`` with proven or default ``passed=False`` and no typed "
            "``error=`` (absent, or the literal ``None``) is an untyped failure: the "
            "gate falls back to the generic ``PREFLIGHT_CHECK_FAILED`` code and only the "
            "deprecated free-text ``message`` reaches the caller. The typed form "
            "``error=AuthError(message=..., suggested_action=..., cause=exc)"
            ".to_failure_details()`` carries category / code / audience / retryable / "
            "suggested_action to the Automation Engine and the UI.\n"
            "\n"
            "Supported SDK public imports, literal values, local boolean bindings, "
            "and the default false value are recognized. An unresolved dynamic "
            "``passed`` without an error produces P065 instead of a proven violation. "
            "A locally-defined non-SDK class named ``PreflightCheck`` is not flagged."
        ),
        help_uri=f"{_HELP_BASE}#p034",
    ),
    RuleDefinition(
        id="P035",
        scope=RuleScope.APP,
        name="PreflightMetadataContractParity",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.15.0",
        rationale=(
            "On the gate path metadata is rebuilt from the extraction input's "
            "model_dump, so a metadata key that is not a field on any entrypoint Input "
            "contract is silently absent — a defensive input.metadata.get(key, default) "
            "read then passes vacuously with the wrong config. No runtime signal can "
            "catch this class of silent drift; the static check is the only guard."
        ),
        short_description=(
            "A preflight_check metadata key is not declared on any entrypoint Input contract"
        ),
        full_description=(
            "The preflight gate does not forward the live UI form: it rebuilds "
            "``PreflightInput.metadata`` from the extraction input's ``model_dump()`` "
            "(``_config_from_snapshot``), so only fields declared on an entrypoint "
            "``Input`` contract survive. A key read inside ``preflight_check`` via "
            '``input.metadata.get("key", ...)`` or ``input.metadata["key"]`` that is '
            "absent from the selected convention-based entrypoint input contract is "
            "silently missing on the gate path, so a defensive ``.get(key, default)`` "
            "read passes vacuously with the wrong configuration (e.g. database scoping "
            "silently dropped).\n"
            "\n"
            "When dispatch cannot be narrowed, the existing union fallback applies; "
            "P065 reports unresolved contract definitions. "
            "Remediation: declare the key as a field on the extraction input contract "
            "(matching the UI form), or stop reading it in ``preflight_check``. Keys are "
            "compared to contract field names with underscore/hyphen normalization; field "
            "aliases are not treated as allowed because ``model_dump`` emits field names, "
            "not aliases. The rule does not fire when no entrypoint Input contract is "
            "resolvable or when a contract (or an in-repo ancestor) opts into extra keys "
            "via either ``model_config`` form: "
            '``ConfigDict(extra="allow")`` or ``{"extra": "allow"}``.'
        ),
        help_uri=f"{_HELP_BASE}#p035",
    ),
    RuleDefinition(
        id="P047",
        scope=RuleScope.APP,
        name="PreflightFailureLoggedAsWarning",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.24.0",
        rationale=(
            "The customer-facing run-log view filters at ERROR by default, so a "
            "preflight failure a handler logs at WARNING is invisible on exactly the "
            "runs where the customer needs to see why the source was not ready "
            "(FND-901). The SDK gate emits the single 'Preflight gate outcome' row "
            "and levels it from the verdict — a handler-authored WARNING is both the "
            "wrong level and a duplicate record."
        ),
        short_description=(
            "logger.warning() inside preflight_check — failure invisible under the default ERROR filter"
        ),
        full_description=(
            "A ``logger.warning(...)`` call inside a ``Handler.preflight_check`` "
            "override logs below the customer log view's default ERROR filter, so a "
            "failed probe reported this way never reaches the customer. The gate owns "
            "preflight outcome logging: it emits one ``Preflight gate outcome`` row "
            "per run and logs it at ERROR when the run is blocked or the source is "
            "unverifiable, stamped with ``failure.audience`` from the typed error.\n"
            "\n"
            "Remediation: express the failure through the typed check result — "
            "``PreflightCheck(passed=False, error=<AppError>.to_failure_details())`` — "
            "and delete the warning; use INFO/DEBUG for non-failure progress. "
            "``warning`` and the deprecated ``warn`` alias are both matched, on any "
            "receiver named like a logger (``logger``, ``log``, ``self._log``, "
            "``logging``). Supported class handlers, module callbacks, and directly "
            "resolvable helpers are scanned; dynamic dispatch requires behavioral evidence."
        ),
        help_uri=f"{_HELP_BASE}#p047",
    ),
)


_CONTRACT_RULES = (
    RuleDefinition(
        id="P052",
        name="PreflightHandlerContract",
        scope=RuleScope.APP,
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Declare SDK PreflightInput and PreflightOutput on every supported handler.",
        full_description="Declare SDK PreflightInput and PreflightOutput on every supported handler.",
        rationale="Customer impact: Missing types and legacy output dictionaries hide contract drift from both UI and workflow consumers.",
        help_uri=f"{_HELP_BASE}#p052",
    ),
    RuleDefinition(
        id="P053",
        name="PreflightFailureAction",
        scope=RuleScope.APP,
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Provide nonblank failure messages and audience-appropriate suggested actions.",
        full_description="Provide nonblank failure messages and audience-appropriate suggested actions.",
        rationale="Customer impact: A typed error with no action still leaves a blocked workflow without a usable next step.",
        help_uri=f"{_HELP_BASE}#p053",
    ),
    RuleDefinition(
        id="P054",
        name="PreflightExpectedFailureRaised",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Return expected typed preflight failures rather than letting them escape.",
        full_description="Return expected typed preflight failures rather than letting them escape.",
        rationale="The target origin-based gate applies hard mode to handler raises; a raised transient is no longer a fail-open request.",
        help_uri=f"{_HELP_BASE}#p054",
    ),
    RuleDefinition(
        id="P055",
        name="PreflightVerdictAggregation",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Keep READY, PARTIAL and NOT_READY consistent with check outcomes.",
        full_description="Keep READY, PARTIAL and NOT_READY consistent with check outcomes.",
        rationale="Advisory failures must not become mandatory blocks and a successful status must not hide failed checks.",
        help_uri=f"{_HELP_BASE}#p055",
    ),
    RuleDefinition(
        id="P056",
        name="PreflightGateInputParity",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Preserve the selected entrypoint and supply routable credentials before the gate.",
        full_description="Preserve the selected entrypoint and supply routable credentials before the gate.",
        rationale="The injected gate runs before workflow-body normalization, so a UI check can succeed while the gate sees different inputs.",
        help_uri=f"{_HELP_BASE}#p056",
    ),
    RuleDefinition(
        id="P057",
        name="PreflightBlockingProbe",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Keep source probes awaitable and bounded across every connection phase.",
        full_description="Keep source probes awaitable and bounded across every connection phase.",
        rationale="Blocking I/O or unbounded executor waits can outlive the gate and stall worker activities.",
        help_uri=f"{_HELP_BASE}#p057",
    ),
    RuleDefinition(
        id="P058",
        name="PreflightBudgetOverride",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Keep probe and retry deadlines inside the remaining gate budget.",
        full_description="Keep probe and retry deadlines inside the remaining gate budget.",
        rationale="Floors, extra margins and equal nested timeout boundaries turn healthy probes into timeout races.",
        help_uri=f"{_HELP_BASE}#p058",
    ),
    RuleDefinition(
        id="P059",
        name="PreflightCancellationCleanup",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Release owned preflight resources without blocking the event loop.",
        full_description="Release owned preflight resources without blocking the event loop.",
        rationale="Cancellation of an await does not terminate a driver thread or release its resources.",
        help_uri=f"{_HELP_BASE}#p059",
    ),
    RuleDefinition(
        id="P060",
        name="PreflightFailureExposure",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Keep raw exception and credential values out of preflight outputs and logs.",
        full_description="Keep raw exception and credential values out of preflight outputs and logs.",
        rationale="Typed wire fields and traceback locals are independent channels through which secrets can escape.",
        help_uri=f"{_HELP_BASE}#p060",
    ),
    RuleDefinition(
        id="P061",
        name="PreflightRemovedGateContract",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Migrate removed mode overrides and private gate-classification helpers.",
        full_description="Migrate removed mode overrides and private gate-classification helpers.",
        rationale="SDK PR #3685 removes the old gate contract. Until its release floor is established this is an upgrade advisory, not proof of current incompatibility.",
        help_uri=f"{_HELP_BASE}#p061",
    ),
    RuleDefinition(
        id="P062",
        name="PreflightBehaviorContract",
        scope=RuleScope.APP,
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.TEST,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Execute registered real-handler scenarios for each applicable entrypoint.",
        full_description="Execute registered real-handler scenarios for each applicable entrypoint.",
        rationale="Customer impact: Static shape checks cannot prove verdict semantics, probe coverage, recovery, or resource lifetime. Missing and skipped scenarios are incomplete evidence.",
        help_uri=f"{_HELP_BASE}#p062",
    ),
    RuleDefinition(
        id="P063",
        name="PreflightWorkflowEnforcement",
        scope=RuleScope.SDK,
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.TEST,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Verify gate enforcement through real Temporal workflow histories.",
        full_description="Verify gate enforcement through real Temporal workflow histories.",
        rationale="Customer impact: Only execution history can prove extraction was never scheduled after a hard gate failure, including activity death.",
        help_uri=f"{_HELP_BASE}#p063",
    ),
    RuleDefinition(
        id="P064",
        name="PreflightExitEvidence",
        scope=RuleScope.SDK,
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.TEST,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Verify typed verdicts, outcome fields and safe evidence handoff on every exit.",
        full_description="Verify typed verdicts, outcome fields and safe evidence handoff on every exit.",
        rationale="Customer impact: Activity and workflow failures must preserve cause and status, and logging must not silently discard the evidence.",
        help_uri=f"{_HELP_BASE}#p064",
    ),
    RuleDefinition(
        id="P065",
        name="PreflightAnalysisCoverage",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Report unresolved preflight dispatch and contracts instead of a clean result.",
        full_description="Report unresolved preflight dispatch and contracts instead of a clean result.",
        rationale="An undiscovered handler or unresolved contract must not be mistaken for conforming code.",
        help_uri=f"{_HELP_BASE}#p065",
    ),
    RuleDefinition(
        id="P066",
        name="DeprecatedPartialPreflight",
        scope=RuleScope.APP,
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Replace deprecated PARTIAL preflight results with an explicit readiness decision.",
        full_description="PARTIAL is deprecated for app preflight results. Return NOT_READY for blocking failures or READY when extraction can proceed, preserving truthful typed check evidence. Recognizes literal and enum values, conditional expressions, and single local assignments in supported handler paths. Dynamic construction requires behavioral validation.",
        rationale="Customer impact: PARTIAL allows extraction to proceed and can conceal a blocking source failure behind a degraded verdict. Explicit readiness decisions prevent this ambiguity.",
        help_uri=f"{_HELP_BASE}#p066",
    ),
)

_GUIDE_BASE = (
    "https://github.com/atlanhq/application-sdk/blob/main/"
    "packages/conformance/conformance/docs/preflight-guide.md"
)

RULES = tuple(
    rule.model_copy(
        update={
            "full_description": (
                f"{rule.full_description}\n\n"
                f"[Investigation, remediation and verification guide]({_GUIDE_BASE}#{rule.id.lower()})."
            )
        }
    )
    for rule in _EXISTING_RULES + _CONTRACT_RULES
)
