# Preflight conformance specification

Status: conformance implementation and remaining acceptance requirements, 2026-09-08. P052–P065 are registered as WARN rules. Static checks run by default; behavioral checks require `--with-tests` and app/SDK scenario adapters. SDK production behavior is unchanged.

The implementation uses bounded static analysis, not general Python execution analysis. It follows direct module helpers, imported helpers, same-class methods, and imported error inheritance. Dynamic factories, arbitrary client instances, decorator wrappers, and semantic recovery still require behavioral adapters. The full acceptance criteria below describe the intended coverage; registration of a TEST rule is not proof that every SDK or app scenario has been implemented or executed.

## Objective and scope

Ensure apps use SDK preflights correctly: the right inputs reach the handler, checks produce truthful and actionable verdicts within a bounded lifetime, and the SDK enforces those verdicts consistently before extraction.

Cover app handlers, their reachable preflight helpers and clients, SDK gate registration and execution, and preflight response and outcome contracts. Inspect extraction only to establish whether a preflight checks the capabilities, resources, credentials, and fallback conditions the selected workflow actually uses. General extraction error handling, publishing, and connector remediation are outside this implementation scope.

Sources:

- [CONNECT-733](https://linear.app/atlan-epd/issue/CONNECT-733): typed contract and mandatory/advisory decisions.
- [CONNECT-769](https://linear.app/atlan-epd/issue/CONNECT-769): typed-error rollout and enforcement.
- [CONNECT-812](https://linear.app/atlan-epd/issue/CONNECT-812): pattern registry, app intakes, and September 6 and September 7 comments.
- [SDK PR #3685](https://github.com/atlanhq/application-sdk/pull/3685): enforcement by failure origin, workflow enforcement after activity failure, and typed exit payloads.

Baseline source inspected: SDK checkout `c4721ab6`. PR #3685 was open at `95677f263e1add545664d710ee36aba8c6551432`, with no merge date, when this specification was written. Its reported production reproductions are evidence in the PR, not reproductions performed for this document.

## Contract and policy decisions

### Results and actions

Handlers accept `PreflightInput` and return `PreflightOutput`. This applies to supported class handlers and per-entrypoint callback functions, with the context parameter permitted where required by dispatch.

Every emitted failed check must carry `FailureDetails` with a nonblank message, category, code, audience, and explicit resolved retryability. Accept SDK-supported construction through an `AppError` converted to `FailureDetails`; do not require one spelling when a supported runtime coercion has the same result. Validate the resulting object, not just the presence of an `error=` expression.

Every failed check must also provide a nonblank `suggested_action`. For USER errors, describe a concrete action the user can take. For PLATFORM and APP_OWNER errors, provide a safe next step appropriate to that ownership, such as retrying later or contacting support with the workflow reference. Do not direct customers to change credentials or permissions for an internal fault. Static checks establish missing values; behavioral fixtures and review establish relevance. Generic filler does not satisfy the acceptance criteria.

A `NOT_READY` result must contain at least one failed, typed check. If an aggregate error is present, it must describe the actual blocking reason rather than an unrelated advisory failure. SDK failures before any handler check can run may carry no checks, but must retain a typed aggregate cause and an explicit verdict or no-verdict state.

`READY` means required checks passed and no advisory failure is being reported. `PARTIAL` preserves failed advisory checks and permits continuation. `NOT_READY` means a required capability was not established and hard mode blocks. The handler owns aggregation; the SDK check model does not currently expose a `required` field. Do not introduce that field as part of conformance without a separate contract decision.

Mandatory checks run in dependency order and short-circuit on the first mandatory failure, following the CONNECT-733 alignment decision. Preserve already completed checks; omit checks that did not run. Permitting additional independent mandatory probes after failure would change that agreed contract and is not part of this specification. Selected-resource aggregation must follow the workflow's supported scope semantics: at least one usable resource permits continuation only where extraction supports that reduced scope. A workflow requiring every selected resource must not inherit an at-least-one rule blindly.

### Transients and continuation

Expected source failures return structured results. A transient is eligible for `PARTIAL` only when a tested extraction retry or fallback can tolerate that condition within its declared bounds. The fixture must exercise that recovery with the same injected source condition. If that evidence is absent, do not prescribe `PARTIAL` solely because the error category is transient or `retryable` is true.

For a required capability that remains unavailable after the permitted probe/retry budget, return a typed `NOT_READY`. `retryable` describes the failure; it does not independently authorize continuation or promise that the gate retries it. Under the target PR contract, handler faults produce a verdict on the attempt where they occur, and deliberate blocks remain non-retryable at the gate boundary.

This narrows the blanket transient-to-`PARTIAL` prescription in the September 7 intake. It is a proposed policy for the conformance programme, not a claim that PR #3685 already verifies extraction recovery. Resolve any disagreement with the PR's migration guidance before promoting this behavioral requirement to BLOCK.

### Target SDK enforcement

Under PR #3685, a handler cannot request fail-open by raising `RateLimitedError`, `DependencyUnavailableError`, or another category. The SDK applies mode to handler-originated failures. Hard mode must prevent extraction after a rejected verdict, handler crash, handler deadline overrun, or a running gate attempt whose failure is classified as source-unverifiable by the workflow. Soft mode records `would_block` and proceeds.

SDK infrastructure failures remain distinct. Credential-store transport failures and activity scheduling failures can produce typed no-verdict fail-open outcomes. A definitive credential absence is not equivalent to a credential-store outage. Optional storage verification can return a confirmed blocking verdict even though storage is SDK-owned; failure origin alone does not replace the distinction between a returned storage verdict and a plumbing exception.

A Temporal timeout type is evidence about execution, not proof of a source root cause. Test running-attempt timeouts separately from never-started activities, credential-resolution stalls, store-probe failures, worker loss, and external cancellation. Preserve earlier typed evidence when present; without it, do not label an app deadline as a confirmed customer connectivity failure.

## Existing machinery to extend

| Existing surface | Current behavior | Required change |
| --- | --- | --- |
| P032, reserved preflight activity | STATIC, BLOCK | Retain; cover supported decorator aliases and wrappers without treating an unrelated decorator as SDK `task`. |
| P033, duplicate workflow preflight | STATIC, WARN | Retain; relate the duplicate to the selected workflow. Removal requires a cold-source scenario because a duplicate may have been providing warm-up. |
| P034, untyped failed check | STATIC, WARN | Extend beyond literal `passed=False` where local data flow proves failure. Runtime assertions cover dynamic expressions, factories, and keyword expansion. |
| P035, metadata/input parity | STATIC, WARN | Compare the selected entrypoint's contract, not the union of all contracts. Unresolved contracts must report incomplete analysis. |
| P047, handler warning logs | STATIC, WARN | Retain its existing identity. A log statement cannot substitute for a typed result. Apply safe reachable-helper discovery. |
| Preflight discovery | Primarily class async methods | Include runtime-supported entrypoint functions, aliases, inherited handlers, and reachable helpers. Report unsupported dynamic dispatch. |
| Rule mechanisms | `STATIC` and `TEST` | Keep these mechanisms. Add explicit execution and result ingestion for the new TEST suite; declaring the enum is not execution support. |
| Test outcome capture | Activity-log assertions | Reuse for activity paths; separately capture workflow-emitted outcomes. One logger does not cover both surfaces. |

Implementation references:

- [Handler contract](../../application_sdk/handler/contracts.py) and [handler guidance](../../application_sdk/handler/base.py).
- [Gate activity](../../application_sdk/execution/_temporal/preflight_gate.py) and [workflow boundary](../../application_sdk/app/base.py).
- [Rule definitions](../../packages/conformance/conformance/suite/rules/preflight.py).
- [Discovery](../../packages/conformance/conformance/suite/checks/preflight/_common.py), [typed failure detector](../../packages/conformance/conformance/suite/checks/preflight/_untyped_failure.py), and [metadata parity detector](../../packages/conformance/conformance/suite/checks/preflight/_metadata_parity.py).
- [Rule schema](../../packages/conformance/conformance/suite/schema/disposition.py) and [suite runner](../../packages/conformance/conformance/suite/runner.py).
- [Outcome assertion helpers](../../application_sdk/testing/preflight.py).

## Proposed rule allocation

P043 through P049 from the latest comment are already occupied. The inspected catalog allocates P050 and P051 as well. P052 through P065 below are allocated in the executable catalog. Catalog tests enforce uniqueness.

Use `WARN` and `BLOCK` as enforcement tiers; `error` is the SARIF level corresponding to BLOCK. All new rules start at WARN for a measured adoption period. BLOCK targets require the graduation criteria below. Do not promote heuristic findings merely because a rollout deadline arrives.

| ID and name | Scope / mechanism | Detects or requires | Target tier |
| --- | --- | --- | --- |
| P052 PreflightHandlerContract | APP / STATIC | Missing or incompatible input/output annotations on a discovered handler; legacy result dictionaries instead of the SDK contract where provable. Runtime outputs checked by P062. | BLOCK |
| P053 PreflightFailureAction | APP / STATIC | Provably blank message or suggested action in a failure reaching a preflight result. Resolve inherited defaults and factory arguments; unresolved values defer to P062. | BLOCK for definite omissions |
| P054 PreflightExpectedFailureRaised | APP / STATIC | Expected probe failures escaping as raises, including the old fail-open tuple, typed subclasses, and re-raises. Follow bounded local call paths and exclude exceptions caught and converted before leaving the handler. | BLOCK for resolved paths |
| P055 PreflightVerdictAggregation | APP / STATIC | Provable status contradictions, unconditional READY shortcuts, and aggregation that makes a declared advisory failure blocking. General mandatory/advisory semantics belong to P062. | WARN |
| P056 PreflightGateInputParity | APP / STATIC | App-constructed workflow preflight input missing its known entrypoint; credentials populated only after the gate; provably incompatible routing selectors. Extend P035 rather than duplicate metadata-key checks. | BLOCK for definite mismatches |
| P057 PreflightBlockingProbe | APP / STATIC | Known blocking driver calls, sleeps, HTTP requests, or cleanup directly on the preflight event loop; executor work with no recognized surrounding deadline. | WARN |
| P058 PreflightBudgetOverride | APP / STATIC | Budget floors exceeding the remaining deadline, unreachable inner deadlines, equal nested driver/wrapper boundaries, and retry waits whose proven maximum exceeds remaining time. | WARN |
| P059 PreflightCancellationCleanup | APP / STATIC | Owned resources acquired on a preflight path with missing exceptional cleanup, or blocking cleanup in async code. Resolve delegated ownership before flagging. | WARN |
| P060 PreflightFailureExposure | APP / STATIC | Raw exception interpolation into preflight wire fields or traceback diagnostics that can expose credential-bearing values on the preflight path. Reuse existing secret/error checks where applicable. | BLOCK for proven unsafe paths |
| P061 PreflightRemovedGateContract | APP / STATIC | Executable/deployment references to the removed env override or private category-based gate helpers under the target SDK contract. Tests intentionally verifying removal are excluded. | BLOCK after SDK applicability is established |
| P062 PreflightBehaviorContract | APP / TEST | Real-handler scenarios verify verdicts, typing, actions, input parity, scope/fallback parity, truthful check rows, deadlines, and cleanup. | BLOCK |
| P063 PreflightWorkflowEnforcement | SDK / TEST | Real workflow execution prevents extraction scheduling on hard-mode gate rejection and handles infrastructure/cancellation paths according to contract. | BLOCK |
| P064 PreflightExitEvidence | SDK / TEST | Consistent typed status/check payloads across HTTP, supported SDR, activity and workflow exits; complete outcome schema; safe log-buffer handoff. | BLOCK |
| P065 PreflightAnalysisCoverage | APP / STATIC | Declared preflight entrypoints not analyzed, unresolved dispatch/contract shapes, and absent required scenario registration. Known no-preflight apps are explicitly not applicable, not healthy preflight implementations. | WARN for unresolved analysis; BLOCK for missing required registration after adoption |

P062 can emit separately identified scenario failures under one rule. Do not create an independent rule for every spelling of the same error or every connector. Error-category correctness and preflight/extraction tolerance parity remain behavioral requirements; simple co-occurrence of two error subclasses does not prove misclassification.

## Detector fixtures and counterexamples

Every static detector must include all three columns as executable fixtures. These fixtures are synthetic and contain no customer identifiers or credentials. Unsupported analysis produces a coverage diagnostic, not a fabricated finding or a clean pass.

| Rule | Broken fixture | Corrected fixture | Legitimate counterexample |
| --- | --- | --- | --- |
| P032/P033 | Wrapped SDK task registers the reserved gate or repeats the handler in the same workflow. | SDK owns the single gate; removal verified without duplicate warm-up. | Unrelated decorator or preflight task in an unrelated workflow. |
| P034 | Failed check has no error, including a locally proven dynamic failure. | Failed check resolves to `FailureDetails`. | Factory supplies `error` through keyword expansion; TEST verifies it. |
| P035/P056 | Miner reads crawler-only metadata or obtains its credential only in the entrypoint body. | Selected input contains routing fields before dispatch. | Interactive input intentionally omits entrypoint; credential reference is resolved by SDK. |
| P047 | Warning is the only representation of a failed probe. | Typed result reaches the SDK outcome boundary. | Unrelated operational warning outside the preflight call path. |
| P052 | Handler returns a legacy bool/dict or annotates the wrong input. | SDK input/output annotations and actual SDK result. | Supported callback includes `HandlerContext`; annotation imported under an alias. |
| P053 | Error has blank action or an inherited unset action. | Action is populated by the leaf or constructor. | Constructor omits action but the typed leaf provides a meaningful default. |
| P054 | Helper re-raises a caught transient out of the handler. | Handler converts it to a typed result with scenario-appropriate status. | Helper raises and an enclosing handler catch converts it before return. |
| P055 | Advisory failure drives `all(passed)` to NOT_READY; empty shortcut returns READY without required checks. | Status follows required capabilities and preserves advisory rows. | All checks are mandatory, or local validation legitimately requires no I/O. |
| P057 | Synchronous request or driver call runs inside async handler. | Known bounded async client or bounded off-loop operation. | Helper or async driver enforces the supplied deadline internally. |
| P058 | `max(remaining, 120)` or retry sleep outlives the budget. | Per-operation bounds fit remaining time with cleanup headroom. | Fixed cap is smaller than remaining time and enforced by an outer deadline. |
| P059 | Cancelled connect leaks its owned engine; async close calls blocking dispose. | Explicit ownership and bounded off-loop cleanup. | Shared pool lifetime is owned and closed by a different component. |
| P060 | Exception containing a synthetic DSN reaches message, action, or traceback locals. | Supported redaction and safe fixed messages preserve typed attribution. | Fixed non-sensitive message with separately sanitized cause. |
| P061 | Deployment sets the removed override or test imports removed internals. | App class declares mode; tests exercise public behavior. | Negative compatibility fixture quotes the removed name without using it. |
| P065 | Per-entrypoint callback exists but no detector visits it. | Every applicable callback is discovered and has scenarios. | App has no preflight by supported design and is explicitly reported as such. |

## Executable behavioral suite

### App adapter and isolation

Define a small test adapter per app and entrypoint. It supplies representative SDK inputs, the real registered handler/callback, fake source-client operations, expected error codes/audiences, required and advisory scenario roles, and any supported recovery/fallback. Scenario roles are test metadata, not additions to `PreflightCheck`.

Patch at the external source-client boundary. Do not replace `preflight_check` or manufacture its output in app conformance tests. Include resolved and inline credential paths using synthetic values. SDK tests may use synthetic handlers to isolate gate behavior.

Run tests without production credentials or live customer systems. Isolate deliberately blocking I/O, cancellation-resistant work, and worker-loss scenarios in disposable subprocesses with a parent-enforced deadline and teardown. A hung test must not hang CI. Track both handler return and remaining background work; cancelling an await does not establish that a thread or remote query stopped.

### P062 required scenarios

| Scenario | Required observation |
| --- | --- |
| Healthy mandatory checks | READY with actual successful probes; expected scope and credentials used. |
| Mandatory authentication/permission failure | NOT_READY, typed cause and action, no later mandatory or dependent probes; completed rows preserved. |
| Advisory failure | PARTIAL, typed failed row, correct action; required successful rows retained. |
| Recoverable transient | PARTIAL only for the declared tolerated condition; same-condition extraction retry/fallback fixture succeeds within bounds. |
| Persistent required-source failure | NOT_READY after bounded attempts; do not relabel source evidence as SDK plumbing to proceed. |
| Mixed selected resources | Status matches the selected workflow's supported scope; zero usable required scope blocks. |
| Extraction fallback | Test both tolerated and non-tolerated status codes/exceptions; preflight and extraction decisions agree in both directions. |
| Credential and entrypoint shapes | Crawler/miner, direct/agent, inline/reference, and connection-derived cases where supported resolve equivalent intended context before checks. |
| No probe / skipped probe | Required remote capability cannot pass without evidence; unexecuted checks omitted; legitimate local validation remains allowed. |
| Hung DNS/connect/TLS/read | Handler returns by its budget plus documented small test scheduling tolerance, with typed failure and correct ownership. Proven source timeout may use SOURCE_UNAVAILABLE; wrapper-only deadline uses TIMEOUT/app attribution unless evidence supports more. |
| Cancellation and cleanup | Cancellation is not swallowed into success; owned resources have bounded cleanup; no uncontrolled residual test processes/threads. |
| Budget exhaustion and retry | Connect plus probes plus retry waits plus cleanup fit remaining time; Retry-After and driver/wrapper boundaries cannot silently reset the deadline. |
| Typed/actionable/safe output | All failed rows meet the result contract; aggregate cause matches blocker; synthetic secret absent from message, action, evidence, cause, and captured logs. |

The adapter must explicitly mark unsupported scenario families with a reason. Skips, xfails, zero collected tests, missing adapters, or fixture setup failures cannot establish conformance. Measure evidence coverage per entrypoint and scenario rather than test-file presence.

### P063 SDK enforcement matrix

Execute a real Temporal test workflow through the SDK wrapper. Register a harmless extraction sentinel activity. In every hard-mode block case, assert the workflow fails with the preflight error and inspect history to prove the extraction sentinel was never scheduled. In permitted cases, assert it executes exactly once. Mocked `execute_activity` tests remain useful unit coverage but are insufficient for this guarantee.

Cover READY, PARTIAL, NOT_READY, each typed handler-origin failure family, untyped handler crash, awaitable overrun, cancellation-resistant probe, and a running attempt terminated by Temporal. Include evidence present/absent and serialized model/dict forms. Cover never-started activities, credential absence versus vault outage, bounded credential-resolution overrun, storage exception versus returned storage verdict, external workflow cancellation, and old-history replay behavior independently.

Run the matrix in soft and hard mode and with relevant attempt counts. Assert mode agreement between worker and workflow and that retry behavior matches the target contract. A source fault must not consume an extra gate attempt merely because it raised rather than returned. SDK infrastructure retries must not discard evidence. Test that a changed failure policy does not introduce replay nondeterminism for existing workflow histories.

### P064 SDK exit and evidence matrix

Validate HTTP success and raised-error paths, supported SDR dispatch, returned activity results, deliberate activity blocks, retry markers, plumbing failures, and workflow-built blocks after activity death. Assert status, checks, primary typed cause, audience, action, and message precedence. Infrastructure no-verdict payloads must not claim a readiness verdict.

For PR #3685, preserve existing HTTP status and legacy `detail` compatibility while checking the additive typed body. Keep legacy successful response compatibility tests. Verify all declared outcome keys on activity and workflow emissions, failed-check codes on proceeded advisory results, and the unmeasured duration sentinel. Do not equate an empty check matrix with a healthy gate.

Exercise log-buffer flush triggers from worker, workflow, and no-running-loop contexts. Assert accepted records remain buffered or are handed off safely; do not claim exactly-once durable delivery during process death. Test attempt-aware consumer selection so an orphaned earlier verdict cannot override the accepted final attempt. Reconcile consumer deduplication with the actual attempt/final-outcome contract rather than prescribing `(workflow_run_id, outcome)` universally.

### Runner integration

Add a TEST execution stage to the full conformance invocation using the existing findings/SARIF model. Collect scenario results and map them to P062, P063, or P064. Preserve a static-only invocation that runs no tests and reports TEST coverage as not evaluated; an explicit mode or flag is implementation work, not an existing CLI capability. Specify the adapter API, test markers, subprocess timeout, and result schema in the harness implementation PR before app adoption; none is an existing public API promised by this document.

The full gate must distinguish a passing executed scenario from missing, skipped, failed-to-run, or unsupported evidence. Do not add a new rule mechanism or silently derive a pass from the absence of findings. Preserve existing suppression behavior; do not require inline source comments as the adoption mechanism.

## Registry coverage ledger

The registry's main table and comments reuse identifiers. This document qualifies them by source rather than overwriting history: `Kafka PF-32..34` refers to the September 6 comment, and `Gate PF-32..36` to September 7. Pattern acceptance and current app deployment status are not inferred from an intake's Done status.

| Registry finding | Coverage or disposition |
| --- | --- |
| PF-01, PF-19, PF-31 | P062 scope/permission/fallback parity, both false-block and false-pass directions. Static semantic proof deferred. |
| PF-02, PF-13, PF-14 | P056 and strengthened P035; P062 verifies entrypoint and credential routing. Arbitrary async credential derivation is not fixed by a connection-field validator. |
| PF-03 | P032/P033 provenance improvements and P065 discovery coverage. |
| PF-04 | Non-finding: absent interactive entrypoint remains legal; P056 must include it as a counterexample. |
| PF-05, PF-06 | P062 cold-source and duplicate-removal scenarios. Old retry-warms-source advice is version-specific and must not be applied unchanged after #3685. |
| PF-07 | P064 attempt/final-outcome evidence and consumer deduplication. |
| PF-08, PF-15, PF-27, PF-30 | P062 checks actual discovery/authorization scope and rejects vacuous remote success. Fast duration is a telemetry signal, not proof. |
| PF-09 | P064 evidence and sink tests; fleet storage/query coverage remains telemetry validation, not an app lint. |
| PF-10 | Explicit limit: bounded preflight cannot prove completion of an unbounded/expensive extraction. P062 documents tested capabilities; no static rule promises full extraction success. |
| PF-11 | P034, P053, P062 enforce typed failed rows and actionable NOT_READY. |
| PF-12 | P055 and P062 mandatory/advisory aggregation. |
| PF-16, PF-24, PF-26 | P057/P058/P059 plus P062 lifetime and deadline scenarios. |
| PF-17, PF-18 | P060 plus P062/P064 synthetic-secret checks on preflight outputs and logging. |
| PF-20 | P054/P062 cover classifier fallthrough reachable from preflight; extraction-only typing is excluded. |
| PF-21 | P054/P062 distinguish source taxonomy, retryability, and continuation; includes ThoughtSpot and Kafka variants. |
| PF-22, PF-23 | P058/P062 bounded transport retries and Retry-After on preflight paths only. |
| PF-25 | Telemetry requirement: account for timeout-censored samples before budget sizing. Completed-run percentiles alone do not certify readiness. |
| PF-28 | P053/P062/P064 enforce useful actions and retained typed attribution. |
| PF-29 | P062 error-owner fixtures. Do not enforce the obsolete equation between fail-open categories and audience after origin-based classification. |
| Kafka PF-32 | P058/P062 driver/request deadline boundary scenarios. |
| Kafka PF-33 | P062/P063 separate app-owned deadline failures from confirmed source causes; mode enforcement must preserve ownership. |
| Kafka PF-34 | P063 pins deliberate-block retry semantics. Changing whether NOT_READY should retry is a separate SDK policy change, not hidden in an app rule. |
| Gate PF-32 | P054/P061 migration diagnostics and P062 transient scenarios. |
| Gate PF-33 | P057/P058/P062 lifetime tests and P063 workflow enforcement. |
| Gate PF-34 | P059/P062 cancellation and cleanup. |
| Gate PF-35 | P063/P064 preserve failure evidence and enforce after a dead attempt. |
| Gate PF-36 | P064 sink handoff regression; independent of app conformance. |
| Looker and Tableau follow-ups | P062 mixed-filter/request-shape parity, real auth probes, transient classification, and bounded recovery. |
| Oracle credential flattening | P056/P062 supported credential-shape scenarios. |
| Kafka codegen follow-up | P035/P056 cover runtime preflight inputs; general codegen re-adoption is excluded. |
| EP-01, EP-02, EP-03, EP-03b | No standalone extraction rules here. Reuse evidence only when retry/classification/sentinel behavior is reachable from preflight or needed for its tolerance-parity scenario. |

## Delivery order and acceptance

1. Implement discovery coverage and shared scenario/result infrastructure. Preserve existing rule identities and validate provisional IDs against the implementation branch.
2. Strengthen P034; add P052/P053/P054/P061 and the corresponding P062 cases. This establishes typed, actionable, returned verdicts and safe upgrade diagnostics.
3. Add P063/P064 execution and wire tests alongside the target SDK contract. Require actual workflow-history evidence for the no-extraction guarantee.
4. Extend input parity and add scope, fallback, truthful-result, budget, and cleanup coverage. Keep semantic static approximations at WARN.
5. Run a representative app cohort covering SQL, HTTP, multi-entrypoint, direct/agent credentials, cold-start sources, and fallback extraction. Use synthetic reproductions derived from the registry, including negative controls.
6. Promote precise rules to BLOCK only after applicable apps have scenarios, known findings are fixed or explicitly accounted for, detector counterexamples pass, and unresolved-analysis rates are reported. Hard-mode enablement additionally needs a fresh, build-bound runtime measurement; green conformance alone is insufficient.

Version applicability must use the actual released SDK version containing #3685 once known, not an inferred version number. A rule's existing `since` field identifies the conformance package version, not the consumer SDK version; implement consumer applicability explicitly. Before that release, obsolete-contract findings explain upcoming incompatibility rather than claiming current behavior is broken. Returning a typed PARTIAL where justified can migrate before the bump; removed imports and deployment overrides must be addressed for the target version. Unknown SDK version yields an applicability diagnostic.

Each implementation PR must include registry mappings, detector fixtures, scenario evidence, known blind spots, and the precise enforcement tier. No live fleet measurement, app remediation, issue updates, CI workflow edits, or SDK behavior changes are authorized by this specification alone.
