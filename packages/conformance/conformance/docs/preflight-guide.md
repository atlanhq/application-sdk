# Investigating preflight conformance findings

This guide is the investigation contract for agents and reviewers. Start at the reported rule, inspect the exact source revision and SDK version, and trace the handler selected by the affected entrypoint. Use synthetic inputs, errors, identifiers and credentials in reproduction artifacts. Connector names describe implementation patterns; they do not establish that every revision of that connector is defective.

## Evidence and scope

A static finding identifies a supported source pattern. Confirm its reachability and final effect before changing behavior. Absence of findings does not prove conformance: imports, dynamic dispatch, factories and complex control flow can exceed analysis coverage. F019 identifies some unresolved paths, not every possible blind spot.

Separate **violation found** from **not evaluated/unresolved** in reports. Conformance never executes tests: F016 checks that the required scenarios are *defined* (registered, collectable, not skipped, asserting the contract), and whether they *pass* is the test gate's measure. The two are reported by different gates and are not combined. Record the command, revision, SDK version, scenario, expected outcome and observed evidence. See [scenario registration](preflight-testing.md).

F001, F003, F006 and F007 block (SARIF `error`); the other preflight rules warn. F016 warns while the fleet registers its scenarios and is promoted to block once it has. F017 and F018 are retired. The generated [catalog page](rules/preflight.md) is the source of truth for tiers. Under `--exit-zero` an exit code of zero can still include violations and undefined scenarios. A guide is not permission to change gate policy or suppress an unresolved result.

## Shared preflight contract

Use SDK `PreflightInput`, `PreflightOutput`, `PreflightCheck` and typed failure details. Failed checks need stable codes, meaningful messages and audience-appropriate suggested actions. Resolve requirements per entrypoint: a failed mandatory check blocks; a supported advisory failure may retain a typed failed row while returning `READY`. `PARTIAL` is deprecated. Declare which probes are mandatory in behavioral scenarios rather than guessing from names or exception classes.

Retryability alone does not justify returning `READY` after a failed probe. Demonstrate that extraction tolerates or recovers from the condition. Test recovery separately from persistent exhaustion. A Monte Carlo connection failure after retries, for example, needs different evidence from a transient that extraction successfully recovers from. Preserve external cancellation and distinguish failure to start a gate from failure after it starts.

## F001

**Contract:** the SDK owns the reserved `preflight` activity name. A collision can prevent worker startup.

**Investigate:** resolve the task decorator and effective registered name, including wrappers. A similarly named ordinary helper is not a collision. **Fix:** rename the app task or move its applicable probes into the SDK handler after checking callers. **Verify:** construct worker activity registrations and assert unique names. Dynamic registration needs execution evidence.

## F002

**Contract:** avoid competing app-owned and SDK-owned preflight paths; their checks and verdicts can diverge.

**Investigate:** trace callers and compare responsibilities. A task whose name mentions preflight might perform separate work; naming alone does not prove redundancy. **Fix:** consolidate genuine duplicates into the handler, preserving every required probe and workflow behavior. **Verify:** exercise each entrypoint and confirm each required probe runs once. Do not delete a task solely from its name.

## F003

**Contract:** every check that can fail must return typed error details. A free-text message loses actionable failure metadata.

**Investigate:** follow variable verdicts, keyword dictionaries, public SDK re-exports and wrapper factories to the returned failed check. S3 public imports and BigQuery/Fivetran conditional verdicts illustrate why literal-only matching is insufficient. **Fix:** attach the appropriate typed error on failure, preserving successful branches. **Verify:** force each failure branch and assert its code, category, audience, message and action. Static uncertainty requires a scenario, not an assumption that the check always passes.

## F004

**Contract:** metadata consumed by the gate must survive serialization of the selected extraction input.

**Investigate:** identify the entrypoint contract, inherited fields, dump behavior and any supported extras. A field on a different entrypoint is insufficient proof. **Fix:** declare the intended field on the correct contract or remove an obsolete read. **Verify:** compare handler inputs from HTTP and reconstructed workflow inputs using synthetic values. Dynamic keys and unresolved contracts limit static proof.

## F005

**Contract:** returned typed results must carry the failure; a warning log cannot substitute for gate evidence.

**Investigate:** determine whether the warning describes failure, progress or recovery, and whether the SDK emits the final outcome. **Fix:** return typed failure evidence and remove redundant failure logging; preserve useful progress at the appropriate level. A best-effort cleanup helper called from `preflight_check` (close a client, release a session) has no `PreflightCheck` to return: WARNING is forbidden, DEBUG/INFO do not clear E004 even through a redaction helper, and the jointly valid log is sanitized `logger.error` or `logger.critical`; typed failure remains preferred when the caller can carry it. **Verify:** capture blocked and advisory outcomes and inspect their severity and fields. A warning call by itself does not prove the workflow lost its failure.

## F006

**Contract:** supported handlers declare SDK input/output types and return valid SDK output instances.

**Investigate:** resolve aliases, inherited methods and per-entrypoint callbacks. Identically named local classes are not SDK contracts. **Fix:** replace legacy dictionaries or booleans with typed outputs while preserving caller expectations. **Verify:** call the real handler for success and failure, validate serialization and the declared return type. Annotations alone do not validate runtime values.

## F007

**Contract:** errors reaching failed checks carry nonblank messages and useful suggested actions.

**Investigate:** trace the final error through subclass defaults, factories, replacement and conversion. Athena reconstructs some intermediate classifier errors with guidance; Cognos raised-only errors are distinct from returned failed-check errors. Do not count either intermediate shape as proof of missing final guidance. **Fix:** add guidance at the final shared error definition or construction that owns the action. **Verify:** execute all consumers of a shared factory and inspect returned details. Nonblank text is mechanically testable; usefulness requires review against the actual failure and audience.

## F008

**Contract:** expected source failures should produce intentional typed verdicts rather than accidentally escape the handler.

**Investigate:** trace matching exception handlers and distinguish expected failures, programming defects and external cancellation. Check the installed SDK's mode semantics: PR3685 describes the target origin-based behavior, not a release floor. **Fix:** convert expected failures to checks with the correct mandatory/advisory classification. Replace deprecated `PARTIAL` with an explicit readiness decision. **Verify:** test recoverable and persistent failures in supported modes and confirm subsequent extraction behavior.

## F009

**Contract:** aggregation respects mandatory/advisory checks, short-circuit dependencies and typed aggregate failure evidence.

**Investigate:** compare every branch against declared requirements. Mode-style `all(c.passed)` aggregation can incorrectly block advisory failures; Hive-style asymmetric branches need semantic tests. **Fix:** aggregate against explicit requirements and retain the decisive failure. **Verify:** healthy, mandatory, advisory, mixed-resource and short-circuit scenarios. Computed status expressions and source policy may exceed static analysis.

## F010

**Contract:** gate execution receives the intended entrypoint and routable credential/input shape before workflow-body normalization.

**Investigate:** trace construction, snapshot reconstruction, credential resolution and routing. **Fix:** normalize at the supported boundary shared by gate and extraction. **Verify:** exercise crawler/miner or other applicable entrypoints with their distinct synthetic credential shapes. Static presence of a field does not prove credentials resolve successfully.

## F011

**Contract:** probes remain awaitable and bounded across connection, authentication, query and fetch phases.

**Investigate:** inspect the underlying driver, not just an async wrapper. Confirm whether a flagged call actually blocks. **Fix:** use supported async operations or bounded offloading with driver-level deadlines. **Verify:** hang each phase, confirm event-loop progress and bounded completion. Cancelling a thread await does not establish termination of the thread.

## F012

**Contract:** attempts, retries and cleanup fit inside the remaining gate budget.

**Investigate:** trace elapsed time, timeout units, floors, added margins and nested deadlines. Equal outer/inner boundaries can race. **Fix:** allocate smaller inner deadlines and leave cleanup time; stop retrying when no useful budget remains. **Verify:** short budgets, retry exhaustion and boundary timing with a controlled clock where possible. A syntactically suspicious timeout needs unit and driver-semantics confirmation.

## F013

**Contract:** owned connections, tasks and threads are released on success, failure and cancellation without blocking the event loop.

**Investigate:** establish resource ownership and whether close methods are synchronous, asynchronous or already offloaded. **Fix:** use supported cleanup with bounded lifetime; preserve cancellation propagation. **Verify:** cancel during each acquisition/use phase and observe no surviving owned work or resources. A returned coroutine or elapsed-time assertion alone cannot prove cleanup.

## F014

**Contract:** output fields and logs exclude raw secrets and unsafe exception representations.

**Investigate:** follow values to serialized checks, aggregate errors and traceback locals without exposing real credentials. **Fix:** use safe messages and SDK sanitization at the relevant boundary; retain stable diagnostic codes. **Verify:** inject unique synthetic secrets into exceptions and inspect every output and log sink. Static matching cannot certify arbitrary sanitizers or every external logging sink. Traceback warnings consider credential reads in the associated try operations or log expression, excluding unrelated locals and nested definitions. They indicate potential exposure when diagnostic rendering is enabled, not proof that a configured sink emits secrets.

## F015

**Contract:** apps must not rely on the inert gate mode override, and must migrate off the gate helpers SDK PR #3685 renamed before those aliases are removed in v3.40.0.

Two different states share this rule. `ATLAN_PREFLIGHT_GATE_MODE` is **already inert**: nothing reads it, and a deployment that still sets it gets a startup warning from the removed-env-var registry. The nine renamed symbols (`resolve_gate_budget_seconds`, `resolve_gate_attempts`, `_is_gate_broken`, `_GATE_BROKEN_CATEGORIES`, `CLASSIFICATION_VERDICT`, `CLASSIFICATION_GATE_BROKEN`, `CLASSIFICATION_SOURCE_UNVERIFIABLE`, `GATE_RETRY`, `UNVERIFIABLE_CHECK_NAME`) still **work**, as deprecated aliases that emit a `DeprecationWarning` naming their replacement, until they are removed in v3.40.0.

**Investigate:** decide which of the two a finding is — an env-var hit is dead configuration to delete now; a symbol import is working code on a deadline. **Fix:** delete the override and declare `App.preflight_gate_mode`; for a symbol, follow the replacement named in its own deprecation notice (`gate_budget_seconds` / `gate_attempts` return `(value, complaint)`; `classify_gate_failure` replaces the private predicate). **Verify:** configuration precedence, and that the migrated code passes with `-W error::DeprecationWarning`. This is a WARN advisory, not proof of current incompatibility: on any SDK before v3.40.0 the aliases resolve and the code runs.

## F016

**Contract:** an app that defines its own `preflight_check` defines every scenario in the required matrix, for each `@entrypoint` it declares, as a pytest-collected test under `tests/` that drives the real handler.

**Investigate:** read the finding: it names the scenario and entrypoint that is missing, or the test whose registration does not count and why — skipped or xfail, declared unsupported, no `assert_preflight_result` (or `assert_probe_lifetime` for hung_probe, cancellation_cleanup and budget_retry), a scenario or entrypoint outside the matrix, or a marker the static reader cannot resolve. Verify fixtures invoke production handlers, replace source I/O only at controlled boundaries and declare mandatory probes. **Fix:** add meaningful scenarios for healthy, mandatory/advisory failure, recovery/exhaustion, mixed resources, input shapes, no/hung probes, cancellation, budgets and safe typed output. Spell the marker's `rule`, `scenario` and `entrypoint` as literals, directly or through a module-level helper whose body is a single `return` (the `entrypoint_matrix` shape in atlan-metabase-app). **Verify:** `detect --series F` reports no F016, and the test job runs the scenarios; introduce a representative defect and show the scenario fails in the test job. A marker or hand-built expected output is not proof of handler behavior — defining the scenario is conformance, and the test gate is what proves it passes.

## F017

**Contract:** retired in 0.39.0, removed in 0.40.0; F017 no longer fires.

**Investigate:** nothing to investigate for a finding: there are none. A `# conformance: ignore[F017]` directive suppresses nothing and is reported by F020. **Fix:** delete the directive. **Verify:** F020 no longer reports it. Gate enforcement through workflow histories is asserted in the SDK's own tests (`tests/unit/app/test_preflight_gate.py`); the rule was SDK-scoped and only restated them.

## F018

**Contract:** retired in 0.39.0, removed in 0.40.0; F018 no longer fires.

**Investigate:** nothing to investigate for a finding: there are none. A `# conformance: ignore[F018]` directive suppresses nothing and is reported by F020. **Fix:** delete the directive. **Verify:** F020 no longer reports it. Exit-evidence behaviour is asserted in the SDK's own tests; the rule was SDK-scoped and only restated them.

## F019

**Contract:** unresolved dispatch, imports or contracts remain visible as analysis gaps.

**Investigate:** locate the actual handler and follow registries, dynamic imports or factories. Snowflake-style dispatch requires checking every registered probe. **Fix:** use a supported resolvable pattern where appropriate, or — for a value-level gap, see below — define the full F016 scenario matrix. **Verify:** demonstrate that a known defect on that path is detected. Do not relabel unresolved as compliant or change runtime semantics merely to satisfy static discovery.

### Which F019 findings scenarios clear, and which they do not

F019 covers two different gaps, and only one of them is cleared by defining the scenarios. Read the message: it says which one you have.

A **value-level** gap means the handler was found and analysed, but one expression's value could not be resolved — a computed aggregation passed to `checks=` or a row inside one the analysis cannot read, an expanded `**kwargs` failure constructor, a computed `suggested_action`, an unresolved error expression on a failed or passed row, an untyped `except` clause, a dynamic `passed`. Each of those names a property that `assert_preflight_result` asserts in **every** F016 scenario: failed checks carry a typed `FailureDetails` with a nonblank message and suggested action, passed checks carry none, and the verdict agrees with the mandatory/advisory roles and the short-circuit order. So `detect --series F` drops these findings once the F016 matrix is fully defined for every entrypoint — every scenario registered, unskipped, and calling that assertion. Conformance does not run those tests; the test gate does, and it is the test gate that proves the assertions hold. This is deliberately all-or-nothing across the matrix — there is no per-site-to-scenario mapping, so a partial matrix clears nothing, and neither does a suppressed F016 finding.

A **structural** gap means the analysis never got to the code: a file that would not parse, a `preflight_check` that does not resolve to a supported async SDK handler, a callback bound dynamically, an input contract class that is not in the registry (so metadata parity was never evaluated). No number of defined scenarios closes these, because a test does not tell the analysis what it failed to read. Their messages do not ask for scenarios; they ask for a statically resolvable shape. Fix the shape.

Zero F019 warnings is reachable for a handler whose only gaps are value-level, but *not* for one whose dispatch or contracts the analysis cannot resolve at all — that is a permanent marker until the shape changes.

Both halves of the `checks=` gate are about resolvability, not syntax. A bare
variable — `PreflightOutput(status=NOT_READY, checks=checks)`, the shape the
short-circuit pattern produces — is reported because the analysis cannot read
the roles, the order or the verdicts out of it. Rewrapping it in a list display,
`checks=[*checks]`, is a semantically identical copy and reports the same thing:
the unpacked element is named on the finding. Building each row inline, or in a
helper whose returns resolve, is what makes the list readable; defining the
F016 matrix is what clears it when the rows genuinely cannot be fixed literals.

Re-typing a caught SDK error onto a failed row — `except AppError as exc: ... error=exc.to_failure_details()` — resolves rather than reporting: the clause proves the value is an `AppError`, so the details are typed. Their message and suggested action belong to whichever raise site built the error, and F007 grades them there, not on the row. A clause naming no typed error — `except Exception`, a bare `except:`, a driver class — stays unresolved and says so; narrow it to the `AppError` subclasses the probe raises, or construct a typed error on that path.


## PARTIAL verdicts — reported by B001, not by an F rule

`PreflightStatus.PARTIAL` is deprecated in the SDK, removed in the first minor release after the reference apps stop returning it, anchored at v3.40.0. The gate treats it exactly like READY, so a PARTIAL verdict can conceal a blocking source failure behind a degraded label, and the gate emits a `DeprecationWarning` when a handler returns it.

There is no preflight rule for it. Reading a deprecated SDK enum member is what **B001** `DeprecatedSdkSymbolUsage` reports, fleet-wide, from the deprecated-symbol manifest — carrying the SDK's own migration guidance on the finding. An F-series rule would put a second WARN on the same line, so the preflight series deliberately has none.

**Investigate:** determine whether each failed probe prevents extraction or whether extraction supports proceeding. Inspect the same source operation and recovery path used by extraction.

**Fix:** return NOT_READY for blocking failures and READY when extraction can proceed. Keep failed check evidence typed and actionable; never relabel a failed probe as passed to satisfy the rule.

**Verify:** exercise both outcomes with real-handler scenarios. F016 accepts a PARTIAL result only when every failed check is advisory and the scenario expects PARTIAL; a failed mandatory probe must give NOT_READY.

Known gap: B001 matches the enum member (`PreflightStatus.PARTIAL`), so a raw-string spelling — `PreflightOutput(status="partial")` — is not reported. `status` is typed `PreflightStatus`, so that spelling is already off-contract; it is an accepted gap rather than a rule of its own.

## F020

**Contract:** a `# conformance: ignore[...]` directive that cites P032, P033, P034, P035, P047, F017 or F018 suppresses nothing. The P-ids moved to F001 to F005 when the preflight rules got their own series; F017 and F018 were retired with no replacement. The parser matches ids as plain strings.

**Investigate:** for a moved id, find the finding the directive was written for and confirm it still fires under the new id on the same line. A directive whose finding is gone is dead weight, not a carve-out. **Fix:** replace a moved id with the one named in the message and keep the justification; delete the directive if the finding no longer fires or its rule was retired. **Verify:** rerun `--series F`. F020 disappears, and the renamed rule is suppressed with its justification counted in `atlan/summary.suppressing`.
