"""Preflight-gate rule definitions (F-series, BLDX-1545 and CONNECT-812).

The SDK injects a mandatory ``{app_name}:preflight`` activity as the first step
of every extraction workflow; it runs the app's ``Handler.preflight_check`` and
blocks the run on a ``NOT_READY`` verdict (application-sdk PRs #2361, #2626).
These rules make the gate pattern statically enforceable across the fleet:
they surface a boot-time collision at review time (F001), the app-owned duplicate
that drifts from the gate (F002), untyped failure results that lose their wire
metadata (F003), the silent metadata/contract drift that no runtime signal
can catch (F004), and preflight failures logged below the customer's default
ERROR filter (F005, FND-901).

F006-F020 add the CONNECT-812 contract, lifetime and behavioral rules.

There is deliberately no preflight rule for a ``PARTIAL`` verdict: it is a
read of the deprecated ``PreflightStatus.PARTIAL`` member, which B001 already
reports fleet-wide from the deprecated-symbol manifest, so a preflight-specific
rule would put a second WARN on the same line.

The detector lives in ``suite.checks.preflight`` and runs on the F leg of the
fleet CI matrix. F001-F005 were published as P032-P035 and P047 and moved here
in PR #3710 before any fleet suppression referenced them; the vacated P-ids are
retired and never reused. From here on the rule-id stability policy in
``prescriptions.py`` applies to F-ids unchanged.
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
    "packages/conformance/conformance/docs/rules/preflight.md"
)

_EXISTING_RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="F001",
        canonical_reference=(
            "atlan-mysql-app app/handler.py — the preflight logic is the Handler's own "
            "`preflight_check` method. No @task in the four reference apps registers the "
            "activity name 'preflight'; that name belongs to the SDK gate, and registering it "
            "shadows the gate itself."
        ),
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
        help_uri=f"{_HELP_BASE}#f001",
    ),
    RuleDefinition(
        id="F002",
        canonical_reference=(
            "atlan-metabase-app app/handler.py — `preflight_check` is the single "
            "implementation and app/connector.py declares no preflight-named @task beside it. "
            "Two implementations drift, and only one of them is the one the gate actually "
            "runs."
        ),
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
            "case is F001), the two preflight paths diverge over time. The SDK gate "
            "invokes ``Handler.preflight_check``; the app-owned activity is dead weight "
            "that silently rots.\n"
            "\n"
            "Remediation: delete the app-owned preflight activity and keep the single "
            "``Handler.preflight_check`` implementation the gate calls."
        ),
        help_uri=f"{_HELP_BASE}#f002",
    ),
    RuleDefinition(
        id="F003",
        canonical_reference=(
            "atlan-mysql-app app/handler.py — a failing check is "
            "`PreflightCheck(passed=False, error=AuthError(message=..., suggested_action=..., "
            "cause=e))`. A `passed=False` with no typed error gives the customer a red row "
            "and no reason for it."
        ),
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
            "``passed`` without an error produces F019 instead of a proven violation. "
            "A locally-defined non-SDK class named ``PreflightCheck`` is not flagged."
        ),
        help_uri=f"{_HELP_BASE}#f003",
    ),
    RuleDefinition(
        id="F004",
        canonical_reference=(
            "atlan-openapi-app app/handler.py — `preflight_check` reads only fields the "
            "entrypoint's Input contract declares. A metadata key the contract does not carry "
            "is one the orchestrator has no way to send, so the check silently evaluates an "
            "absent value."
        ),
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
            "F019 reports unresolved contract definitions. "
            "Remediation: declare the key as a field on the extraction input contract "
            "(matching the UI form), or stop reading it in ``preflight_check``. Keys are "
            "compared to contract field names with underscore/hyphen normalization; field "
            "aliases are not treated as allowed because ``model_dump`` emits field names, "
            "not aliases. The rule does not fire when no entrypoint Input contract is "
            "resolvable or when a contract (or an in-repo ancestor) opts into extra keys "
            "via either ``model_config`` form: "
            '``ConfigDict(extra="allow")`` or ``{"extra": "allow"}``.'
        ),
        help_uri=f"{_HELP_BASE}#f004",
    ),
    RuleDefinition(
        id="F005",
        canonical_reference=(
            "atlan-mysql-app app/handler.py — the failed auth probe inside `preflight_check` "
            "logs at DEBUG and puts the customer-facing outcome in the PreflightCheck's typed "
            "error instead. The comment there states why: the gate levels the verdict row "
            "itself, and a handler-authored WARNING is both a duplicate and invisible under "
            "the customer's default ERROR filter."
        ),
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
        help_uri=f"{_HELP_BASE}#f005",
    ),
)


_CONTRACT_RULES = (
    RuleDefinition(
        id="F006",
        canonical_reference=(
            "atlan-metabase-app app/handler.py — `async def preflight_check(self, input: "
            "PreflightInput) -> PreflightOutput`, both types imported from "
            "application_sdk.handler.contracts. The gate and the setup UI both read the "
            "result through those types, so a legacy dict return drifts from both at once."
        ),
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
        help_uri=f"{_HELP_BASE}#f006",
    ),
    RuleDefinition(
        id="F007",
        canonical_reference=(
            "atlan-openapi-app app/handler.py — every failed row is built from a typed error "
            "that sets both message and suggested_action, e.g. "
            "`SpecUrlRequiredError(message=..., suggested_action='Set spec_url to the OpenAPI "
            "spec's HTTPS URL ...').to_failure_details()`, so the blocked customer reads a "
            "next step, not only a reason."
        ),
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
        help_uri=f"{_HELP_BASE}#f007",
    ),
    RuleDefinition(
        id="F008",
        canonical_reference=(
            "atlan-openapi-app app/handler.py — `_check_spec_source` catches AppError and "
            "returns the failed row with `exc.to_failure_details()`; only the gate-transient "
            "categories are re-raised, on purpose, so the gate fails open on a blip instead "
            "of the handler crashing on an expected failure."
        ),
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
        help_uri=f"{_HELP_BASE}#f008",
    ),
    RuleDefinition(
        id="F009",
        canonical_reference=(
            "atlan-openapi-app app/handler.py — the verdict is derived from the same check "
            "list that is returned: any failed name in `_MANDATORY_CHECKS` gives NOT_READY, "
            "otherwise the advisory rows stay visible without flipping the status, so status "
            "and rows cannot contradict each other."
        ),
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
        help_uri=f"{_HELP_BASE}#f009",
    ),
    RuleDefinition(
        id="F010",
        canonical_reference=(
            "atlan-mysql-app tests/unit/test_handler.py — "
            "`test_gate_path_input_gives_the_same_verdict` builds the PreflightInput the gate "
            "builds (credentials, credentials_by_name, entrypoint, timeout_seconds) and "
            "asserts the handler reaches the same verdict as the setup-form path."
        ),
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
        help_uri=f"{_HELP_BASE}#f010",
    ),
    RuleDefinition(
        id="F011",
        canonical_reference=(
            "atlan-openapi-app app/handler.py — the probe is awaited through "
            "`OpenAPIApiClient(timeout=...)`, an async client constructed with a deadline "
            "sized from `input.timeout_seconds`; no synchronous driver call runs on the event "
            "loop and no executor wait is left without a deadline."
        ),
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
        help_uri=f"{_HELP_BASE}#f011",
    ),
    RuleDefinition(
        id="F012",
        canonical_reference=(
            "atlan-openapi-app app/handler.py — `_probe_timeout` returns `max(1.0, min(30.0, "
            "budget * 0.8))`, so the probe's own timeout stays strictly inside the enforced "
            "gate budget; the module comment explains that a floor above the budget makes the "
            "deadline decorative."
        ),
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
        help_uri=f"{_HELP_BASE}#f012",
    ),
    RuleDefinition(
        id="F013",
        canonical_reference=(
            "atlan-mysql-app app/handler.py — `preflight_check` closes its SQLClient in a "
            "`finally: await client.close()`, so cleanup is awaited, bounded and runs on "
            "every exit path, including the typed-failure early return."
        ),
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
        help_uri=f"{_HELP_BASE}#f013",
    ),
    RuleDefinition(
        id="F014",
        canonical_reference=(
            "atlan-openapi-app app/handler.py — failed rows carry `exc.to_failure_details()`, "
            "never `str(exc)` or a traceback, so the redacted and capped `cause_repr` is all "
            "that leaves the handler; tests/unit/test_handler.py pins that a presigned URL's "
            "signature does not reach the check row."
        ),
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
        help_uri=f"{_HELP_BASE}#f014",
    ),
    RuleDefinition(
        id="F015",
        canonical_reference=(
            "application_sdk/execution/_temporal/preflight_gate.py — the gate's live "
            "configuration surface. A manifest key or helper import that this module no "
            "longer reads is dead configuration, and the SDK version it was removed in "
            "decides whether a finding applies."
        ),
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
        help_uri=f"{_HELP_BASE}#f015",
    ),
    RuleDefinition(
        id="F016",
        canonical_reference=(
            "atlan-openapi-app tests/unit/test_handler.py — drives the real "
            "`OpenAPIConnectorHandler.preflight_check` per verdict: READY on a reachable URL, "
            "NOT_READY with typed rows on 403, connect error, redirect and missing spec_url, "
            "and no signature leak on a presigned URL. Registering those under "
            "`pytest.mark.preflight_conformance` is what turns them into F016 coverage."
        ),
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
        help_uri=f"{_HELP_BASE}#f016",
    ),
    RuleDefinition(
        id="F017",
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
        help_uri=f"{_HELP_BASE}#f017",
    ),
    RuleDefinition(
        id="F018",
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
        help_uri=f"{_HELP_BASE}#f018",
    ),
    RuleDefinition(
        id="F019",
        canonical_reference=(
            "atlan-openapi-app app/handler.py — `preflight_check` is an async method on the "
            "Handler subclass, calls helpers defined in the same module, and builds "
            "PreflightCheck rows with literal names: the shape static analysis resolves "
            "fully, so nothing on it is reported as unresolved."
        ),
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
        help_uri=f"{_HELP_BASE}#f019",
    ),
    RuleDefinition(
        id="F020",
        canonical_reference=(
            "atlan-mysql-app app/handler.py — its inline directives cite live ids "
            "(E004) with a named owner and a review date. A directive that cited P034 "
            "now cites F003 the same way; the id is the only part that changes."
        ),
        name="RetiredPreflightSuppression",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.32.0",
        short_description="A conformance suppression cites a retired preflight id (P032-P035, P047).",
        full_description=(
            "The preflight rules moved from the P-series to the F-series: P032-P035 "
            "became F001-F004 and P047 became F005. The suppression parser matches "
            "ids as plain strings, so a ``# conformance: ignore[...]`` directive that "
            "still cites a retired id suppresses nothing and the renamed rule fires "
            "with no hint why. Cite the new id named in the message, keeping the "
            "justification, or delete the directive if the finding it covered is gone."
        ),
        rationale=(
            "Customer impact: a reviewed, justified carve-out silently turns into an "
            "unexplained finding on the next conformance run, and the developer has "
            "no signal that the stale directive is the cause."
        ),
        help_uri=f"{_HELP_BASE}#f020",
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
