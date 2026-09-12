# Investigating preflight conformance findings

This guide is the investigation contract for agents and reviewers. Start at the reported rule, inspect the exact source revision and SDK version, and trace the handler selected by the affected entrypoint. Use synthetic inputs, errors, identifiers and credentials in reproduction artifacts. Connector names describe implementation patterns; they do not establish that every revision of that connector is defective.

## Evidence and scope

A static finding identifies a supported source pattern. Confirm its reachability and final effect before changing behavior. Absence of findings does not prove conformance: imports, dynamic dispatch, factories and complex control flow can exceed analysis coverage. P065 identifies some unresolved paths, not every possible blind spot.

Separate **violation found**, **verified by executed tests**, and **not evaluated/unresolved** in reports. P062–P064 require opted-in, registered scenarios; missing, skipped and failed scenarios are not passing evidence. The presence of assertion helpers does not mean a real handler or Temporal workflow was exercised. Record the command, revision, SDK version, scenario, expected outcome and observed evidence. See [behavioral test registration](preflight-testing.md).

P032 blocks. The other preflight rules currently warn; exit code zero can include violations and missing behavioral coverage. A guide is not permission to change gate policy or suppress an unresolved result.

## Shared preflight contract

Use SDK `PreflightInput`, `PreflightOutput`, `PreflightCheck` and typed failure details. Failed checks need stable codes, meaningful messages and audience-appropriate suggested actions. Resolve requirements per entrypoint: a failed mandatory check blocks; a supported advisory failure may retain a typed failed row while returning `READY`. `PARTIAL` is deprecated. Declare which probes are mandatory in behavioral scenarios rather than guessing from names or exception classes.

Retryability alone does not justify returning `READY` after a failed probe. Demonstrate that extraction tolerates or recovers from the condition. Test recovery separately from persistent exhaustion. A Monte Carlo connection failure after retries, for example, needs different evidence from a transient that extraction successfully recovers from. Preserve external cancellation and distinguish failure to start a gate from failure after it starts.

## P032

**Contract:** the SDK owns the reserved `preflight` activity name. A collision can prevent worker startup.

**Investigate:** resolve the task decorator and effective registered name, including wrappers. A similarly named ordinary helper is not a collision. **Fix:** rename the app task or move its applicable probes into the SDK handler after checking callers. **Verify:** construct worker activity registrations and assert unique names. Dynamic registration needs execution evidence.

## P033

**Contract:** avoid competing app-owned and SDK-owned preflight paths; their checks and verdicts can diverge.

**Investigate:** trace callers and compare responsibilities. A task whose name mentions preflight might perform separate work; naming alone does not prove redundancy. **Fix:** consolidate genuine duplicates into the handler, preserving every required probe and workflow behavior. **Verify:** exercise each entrypoint and confirm each required probe runs once. Do not delete a task solely from its name.

## P034

**Contract:** every check that can fail must return typed error details. A free-text message loses actionable failure metadata.

**Investigate:** follow variable verdicts, keyword dictionaries, public SDK re-exports and wrapper factories to the returned failed check. S3 public imports and BigQuery/Fivetran conditional verdicts illustrate why literal-only matching is insufficient. **Fix:** attach the appropriate typed error on failure, preserving successful branches. **Verify:** force each failure branch and assert its code, category, audience, message and action. Static uncertainty requires a scenario, not an assumption that the check always passes.

## P035

**Contract:** metadata consumed by the gate must survive serialization of the selected extraction input.

**Investigate:** identify the entrypoint contract, inherited fields, dump behavior and any supported extras. A field on a different entrypoint is insufficient proof. **Fix:** declare the intended field on the correct contract or remove an obsolete read. **Verify:** compare handler inputs from HTTP and reconstructed workflow inputs using synthetic values. Dynamic keys and unresolved contracts limit static proof.

## P047

**Contract:** returned typed results must carry the failure; a warning log cannot substitute for gate evidence.

**Investigate:** determine whether the warning describes failure, progress or recovery, and whether the SDK emits the final outcome. **Fix:** return typed failure evidence and remove redundant failure logging; preserve useful progress at the appropriate level. **Verify:** capture blocked and advisory outcomes and inspect their severity and fields. A warning call by itself does not prove the workflow lost its failure.

## P052

**Contract:** supported handlers declare SDK input/output types and return valid SDK output instances.

**Investigate:** resolve aliases, inherited methods and per-entrypoint callbacks. Identically named local classes are not SDK contracts. **Fix:** replace legacy dictionaries or booleans with typed outputs while preserving caller expectations. **Verify:** call the real handler for success and failure, validate serialization and the declared return type. Annotations alone do not validate runtime values.

## P053

**Contract:** errors reaching failed checks carry nonblank messages and useful suggested actions.

**Investigate:** trace the final error through subclass defaults, factories, replacement and conversion. Athena reconstructs some intermediate classifier errors with guidance; Cognos raised-only errors are distinct from returned failed-check errors. Do not count either intermediate shape as proof of missing final guidance. **Fix:** add guidance at the final shared error definition or construction that owns the action. **Verify:** execute all consumers of a shared factory and inspect returned details. Nonblank text is mechanically testable; usefulness requires review against the actual failure and audience.

## P054

**Contract:** expected source failures should produce intentional typed verdicts rather than accidentally escape the handler.

**Investigate:** trace matching exception handlers and distinguish expected failures, programming defects and external cancellation. Check the installed SDK's mode semantics: PR3685 describes the target origin-based behavior, not a release floor. **Fix:** convert expected failures to checks with the correct mandatory/advisory classification. Replace deprecated `PARTIAL` with an explicit readiness decision. **Verify:** test recoverable and persistent failures in supported modes and confirm subsequent extraction behavior.

## P055

**Contract:** aggregation respects mandatory/advisory checks, short-circuit dependencies and typed aggregate failure evidence.

**Investigate:** compare every branch against declared requirements. Mode-style `all(c.passed)` aggregation can incorrectly block advisory failures; Hive-style asymmetric branches need semantic tests. **Fix:** aggregate against explicit requirements and retain the decisive failure. **Verify:** healthy, mandatory, advisory, mixed-resource and short-circuit scenarios. Computed status expressions and source policy may exceed static analysis.

## P056

**Contract:** gate execution receives the intended entrypoint and routable credential/input shape before workflow-body normalization.

**Investigate:** trace construction, snapshot reconstruction, credential resolution and routing. **Fix:** normalize at the supported boundary shared by gate and extraction. **Verify:** exercise crawler/miner or other applicable entrypoints with their distinct synthetic credential shapes. Static presence of a field does not prove credentials resolve successfully.

## P057

**Contract:** probes remain awaitable and bounded across connection, authentication, query and fetch phases.

**Investigate:** inspect the underlying driver, not just an async wrapper. Confirm whether a flagged call actually blocks. **Fix:** use supported async operations or bounded offloading with driver-level deadlines. **Verify:** hang each phase, confirm event-loop progress and bounded completion. Cancelling a thread await does not establish termination of the thread.

## P058

**Contract:** attempts, retries and cleanup fit inside the remaining gate budget.

**Investigate:** trace elapsed time, timeout units, floors, added margins and nested deadlines. Equal outer/inner boundaries can race. **Fix:** allocate smaller inner deadlines and leave cleanup time; stop retrying when no useful budget remains. **Verify:** short budgets, retry exhaustion and boundary timing with a controlled clock where possible. A syntactically suspicious timeout needs unit and driver-semantics confirmation.

## P059

**Contract:** owned connections, tasks and threads are released on success, failure and cancellation without blocking the event loop.

**Investigate:** establish resource ownership and whether close methods are synchronous, asynchronous or already offloaded. **Fix:** use supported cleanup with bounded lifetime; preserve cancellation propagation. **Verify:** cancel during each acquisition/use phase and observe no surviving owned work or resources. A returned coroutine or elapsed-time assertion alone cannot prove cleanup.

## P060

**Contract:** output fields and logs exclude raw secrets and unsafe exception representations.

**Investigate:** follow values to serialized checks, aggregate errors and traceback locals without exposing real credentials. **Fix:** use safe messages and SDK sanitization at the relevant boundary; retain stable diagnostic codes. **Verify:** inject unique synthetic secrets into exceptions and inspect every output and log sink. Static matching cannot certify arbitrary sanitizers or every external logging sink. Traceback warnings consider credential reads in the associated try operations or log expression, excluding unrelated locals and nested definitions. They indicate potential exposure when diagnostic rendering is enabled, not proof that a configured sink emits secrets.

## P061

**Contract:** apps must not rely on removed gate overrides or private classifiers after upgrading past their removal.

**Investigate:** confirm the installed/pinned SDK and the actual removal release before declaring incompatibility. **Fix:** migrate to the supported app gate configuration and public contract for that version. **Verify:** configuration precedence and old/new supported-version behavior. Until the release floor is established, this finding is an upgrade advisory.

## P062

**Contract:** each applicable entrypoint has executed real-handler scenarios covering the required behavior matrix.

**Investigate:** verify fixtures invoke production handlers, replace source I/O only at controlled boundaries and declare mandatory probes. **Fix:** add meaningful scenarios for healthy, mandatory/advisory failure, recovery/exhaustion, mixed resources, input shapes, no/hung probes, cancellation, budgets and safe typed output. **Verify:** introduce a representative defect and show the scenario fails, then passes after correction. A marker or hand-built expected output is not proof of handler behavior.

## P063

**Contract:** SDK workflow histories demonstrate gate enforcement, including failures after the activity starts.

**Investigate:** inspect actual Temporal execution history and the gate failure cause. An unrelated workflow failure cannot prove enforcement. **Fix:** provide SDK integration scenarios covering verdicts, raises, overruns, activity death, credentials, evidence failure, cancellation, modes and replay. **Verify:** assert extraction scheduling and terminal outcomes for each case. Fabricated histories, missing Temporal infrastructure and skipped tests provide no execution proof.

## P064

**Contract:** HTTP, activity and workflow exits preserve typed verdicts, failure precedence and safe outcome evidence.

**Investigate:** capture the actual boundary payload and emitted outcome, including retries and plumbing failures without a verdict. **Fix:** preserve the decisive error and supported legacy behavior through serialization and handoff. **Verify:** registered exit scenarios assert status, checks, action, code, duration, attempt selection and secret exclusion. A synthetic payload validated in isolation does not verify the production handoff.

## P065

**Contract:** unresolved dispatch, imports or contracts remain visible as analysis gaps.

**Investigate:** locate the actual handler and follow registries, dynamic imports or factories. Snowflake-style dispatch requires checking every registered probe. **Fix:** use a supported resolvable pattern where appropriate, improve analysis with regression tests, or add executable scenarios for the unresolved path. **Verify:** demonstrate that a known defect on that path is detected. Do not relabel unresolved as compliant or change runtime semantics merely to satisfy static discovery.


## P066

**Contract:** App preflight results must not use the deprecated PARTIAL status. This rule reports BLOCK/error.

**Investigate:** Determine whether each failed probe prevents extraction or whether extraction supports proceeding. Inspect the same source operation and recovery path used by extraction.

**Fix:** Return NOT_READY for blocking failures and READY when extraction can proceed. Keep failed check evidence typed and actionable; never relabel a failed probe as passed to satisfy the rule.

**Verify:** Exercise both outcomes with real-handler scenarios. P062 rejects PARTIAL at runtime, including dynamically constructed statuses.

Static detection covers supported PreflightOutput construction with literals, enum members, conditional expressions, and single local assignments. Arbitrary factories and mutations require behavioral tests. The SDK enum remains available for compatibility; this is a conformance deprecation.
