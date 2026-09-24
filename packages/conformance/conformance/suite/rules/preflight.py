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

F006-F020 add the CONNECT-812 contract, lifetime and behavioral rules. F021
flags a fixed auth or permission leaf built in a broad except (CONNECT-1358).

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
            "`preflight_check` method. No @task in the three reference apps registers the "
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
            "atlan-mysql-app app/handler.py — the failed auth probe in "
            '`_run_preflight_probes` is `PreflightCheck(name="auth", passed=False, '
            "error=PreflightAuthError(cause=e).to_failure_details())`, where "
            "PreflightAuthError (app/failures.py) is an AuthError subclass that declares "
            "message and suggested_action as class defaults. A `passed=False` with no typed "
            "error gives the customer a red row and no reason for it."
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
            "atlan-openapi-app app/handler.py — `preflight_check` reads no `input.metadata` "
            "key at all: its configuration comes from `input.connection_config` "
            '(`cfg.get("import_type")`, `cfg.get("spec_url")`), so there is no metadata '
            "read for the gate path to drop. A metadata key the entrypoint's Input contract "
            "does not carry is one the orchestrator has no way to send, so the check silently "
            "evaluates an absent value."
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
        rule_interactions=(
            "Meets E004 on a broad catch inside the gate's reach. A best-effort "
            "cleanup helper called from preflight_check (close a client, release a "
            "session) that catches Exception cannot log at WARNING (this rule), and "
            "DEBUG does not clear E004 even through a redaction helper. Use "
            "logger.error (or logger.critical) with the exception routed through a redaction helper "
            "(safe_traceback, sanitize_cause_repr), or return the failure as typed "
            "data. A probe arm that already returns a typed PreflightCheck clears "
            "E004 with no log at all (FND-2628). Found in FND-2569."
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
            "atlan-mysql-app app/handler.py — the advisory `connectivity` check catches the "
            "probe failure and returns it on the failed row, a blip as the retryable leaf "
            "from `transient_failure(e)`, so the verdict stays READY and nothing it expects "
            "escapes preflight_check, where the origin-based gate would block on it."
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
            "atlan-mysql-app app/handler.py — `_run_preflight_probes` writes each verdict "
            "next to the rows that justify it: NOT_READY carries the failed mandatory `auth` "
            "row (`checks=[auth_check]`), and READY carries the passed `auth` row plus the "
            "advisory connectivity row, which may fail without flipping the status. Status "
            "and rows are spelled together, so they cannot contradict each other."
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
            "atlan-metabase-app app/connector.py — the `extract_metadata` @entrypoint "
            "constructs no PreflightInput of its own and leaves preflight to the SDK gate, so "
            "on the workflow path the gate's input (the selected entrypoint plus resolved "
            "credentials) is the only PreflightInput the handler receives. None of the three "
            "reference apps builds a PreflightInput "
            "inside an @entrypoint."
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
            "atlan-mysql-app app/handler.py — `preflight_check` bounds the probes with "
            "`asyncio.wait_for(self._run_preflight_probes(input, deadline), "
            "timeout=deadline)`, where `deadline = _probe_deadline(input.timeout_seconds)` is "
            "80% of the budget the gate hands in (no deadline when the gate supplies none). "
            "No floor or margin is added on top of "
            "`input.timeout_seconds`, so the probe gives up before the gate cancels it; that "
            "timeout argument is the site F012 grades."
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
            "atlan-mysql-app app/handler.py — `_run_preflight_probes` (which "
            "`preflight_check` runs under `asyncio.wait_for`) closes its SQLClient in a "
            "`finally: await client.close()`, so cleanup is awaited and runs on every exit "
            "path, including the typed-failure early return."
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
            "configuration surface, and the deprecated-alias block at the end of it. "
            "Two states share this rule: ATLAN_PREFLIGHT_GATE_MODE is already inert, so "
            "a deployment still setting it is dead configuration to delete now; the nine "
            "symbols PR #3685 renamed still resolve, as aliases that warn and are removed "
            "in v3.40.0, so an import of one is working code on a deadline rather than an "
            "incompatibility. Correct looks like the posture declared on "
            "App.preflight_gate_mode and the replacement each deprecation notice names."
        ),
        name="PreflightRemovedGateContract",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Migrate the inert mode override and the renamed gate-classification helpers.",
        full_description="Migrate the inert mode override and the renamed gate-classification helpers.",
        rationale="SDK PR #3685 renamed the old gate contract. The nine affected symbols are served as deprecated aliases until v3.40.0 and ATLAN_PREFLIGHT_GATE_MODE no longer does anything, so a hit is a migration window rather than proof of current incompatibility — WARN, not BLOCK.",
        help_uri=f"{_HELP_BASE}#f015",
    ),
    RuleDefinition(
        id="F016",
        canonical_reference=(
            "atlan-openapi-app tests/unit/test_preflight_conformance.py — each required "
            "scenario (healthy, mandatory_failure, recoverable_transient, hung_probe, "
            "cancellation_cleanup and the rest) is a test that drives the real "
            "`OpenAPIConnectorHandler.preflight_check` and is registered with "
            '`@pytest.mark.preflight_conformance(rule="F016", scenario=...)`. The marker, '
            "not the file's presence, is what a `--with-tests` run counts as coverage."
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
            "atlan-mysql-app app/handler.py — every PreflightOutput in "
            "`_run_preflight_probes` spells its checks list inline, from PreflightCheck "
            "constructions or a same-class helper (`_check_connectivity`), never an "
            "accumulator, so static analysis resolves every row and its mandatory/advisory "
            "role. The comment above the NOT_READY return cites F019 as the reason."
        ),
        name="PreflightAnalysisCoverage",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.27.0",
        short_description="Report unresolved preflight dispatch and contracts instead of a clean result.",
        full_description=(
            "Report unresolved preflight dispatch and contracts instead of a clean "
            "result. Two kinds of gap are reported, and only one of them is "
            "clearable by executing tests. A *value-level* gap — a computed "
            "aggregation or an unresolvable row inside one, an expanded failure "
            "constructor, an unresolved error expression, a dynamic ``passed`` — "
            "names a property F016 asserts on "
            "every executed scenario, so a ``--with-tests`` run whose F016 matrix "
            "is complete and passing drops it. A *structural* gap — an unparsed "
            "file, a preflight_check the analysis never resolved, a dynamically "
            "bound callback, an input contract class that is not in the registry — "
            "stands regardless of how many scenarios pass, because execution does "
            "not tell the analysis what it failed to read; clear those by making "
            "the code statically resolvable."
        ),
        rationale="An undiscovered handler or unresolved contract must not be mistaken for conforming code.",
        help_uri=f"{_HELP_BASE}#f019",
    ),
    RuleDefinition(
        id="F020",
        canonical_reference=(
            "atlan-metabase-app app/qualified_names.py — its inline conformance "
            "directives name a live rule id (P028) and carry a written "
            "justification. A directive that cited P034 now cites F003 the same way, "
            "justification kept; the id is the only part that changes."
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
    RuleDefinition(
        id="F021",
        canonical_reference=(
            "atlan-metabase-app app/handler.py — the authenticationCheck probe "
            "catches `(InvalidInputError, AuthError)` first and returns that typed "
            "error, and only its trailing `except Exception` builds a leaf, "
            "`MetabaseSourceUnavailableError`, so a failure it cannot name is never "
            "reported as a credential or grant problem."
        ),
        name="PreflightFixedLeafInBroadExcept",
        scope=RuleScope.APP,
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        orthogonal_gate="tests",
        since="0.39.0",
        short_description="Classify a broadly caught preflight failure before naming it an auth or permission problem.",
        full_description=(
            "A broad ``except`` in ``preflight_check`` or a helper it reaches "
            "builds an ``AuthError`` or ``AppPermissionDeniedError`` subclass "
            "without testing the caught exception or passing it to a classifier. "
            "Classify it first — ``application_sdk.errors.classify_http_exception`` "
            "for httpx failures, or an ``isinstance`` chain — and fall back to a leaf "
            "that does not blame the customer (``InternalError`` when the cause is "
            "unknown), or narrow the except clause."
        ),
        rationale=(
            "Customer impact: an empty credential, a DNS failure or a source 500 is "
            "reported to the customer as a missing grant, and the ticket chases "
            "source-side permissions that were never the problem."
        ),
        help_uri=f"{_HELP_BASE}#f021",
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
