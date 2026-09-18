# Preflight conformance

Current policy: the SDK deprecates `PreflightStatus.PARTIAL`. Removal lands in the first minor release after the reference apps stop returning it, anchored at v3.40.0; the gate emits a `DeprecationWarning` when a handler returns it. A PARTIAL verdict is reported by B001 as a deprecated-enum-member read, not by a preflight rule; F020 was dropped and its id retired to avoid two WARN findings on one line. F016 scenarios accept PARTIAL only when every failed check is advisory; use NOT_READY for mandatory failures and READY for supported continuation, retaining truthful typed check evidence. The gate's treatment of PARTIAL is unchanged until removal. There are 20 preflight rules (17 static, 3 behavioral), with 7 BLOCK and 13 WARN; the generated [catalog page](rules/preflight.md) is the source of truth for tiers.

F006–F015 and F019 add static diagnostics for handler types, actionable errors, raised expected failures, verdict consistency, entrypoint inputs, blocking probes, budgets, cleanup, sensitive failure text, removed gate configuration, and incomplete analysis. F021 flags a suppression that still cites a retired P-id. Existing F001–F004 and F005 continue to apply. F001, F003, F006, F007 and F016–F018 use BLOCK (SARIF `error`). F003/F006/F007 enforce typed failures, handler contracts, and definite missing failure guidance. Behavioral rules require complete passing scenarios when explicitly run with `--with-tests`; missing or skipped scenarios are errors. Static-only runs still report behavioral checks as not evaluated. Other preflight rules remain WARN because their findings include heuristics, unresolved analysis, or SDK-version-dependent advice. `--exit-zero` preserves error findings while returning a successful process exit for soft enforcement.

Run static checks:

```bash
uv run atlan-application-sdk-conformance detect --repo . --series F --static --output preflight.sarif
```

Run registered behavior scenarios in the app's existing test environment:

```bash
uv run atlan-application-sdk-conformance detect --repo . --rule F016 --with-tests --test-python .venv/bin/python --test-timeout 120 --output preflight.sarif
```

Use `--rule F017,F018 --scope sdk --with-tests` for SDK scenarios. Install the package's existing `test` extra in the test environment. Static use does not require pytest. Test execution is opt-in because it imports and executes repository tests, including their fixtures. Use synthetic source adapters and isolated infrastructure.

Mark tests with `pytest.mark.preflight_conformance(rule="F016", scenario="healthy", entrypoint="crawler")`. The authoritative scenario names are in `conformance.preflight_testing.SCENARIOS`. Each selected entrypoint needs the complete applicable matrix. Unregistered, skipped, unsupported, failed, timed-out, and unexpectedly passing xfail scenarios do not establish conformance. A missing matrix emits findings; a static run records `not_evaluated` in SARIF `runs[].properties["atlan/preflightTests"]`.

F016 tests invoke the real handler using an app-owned source adapter and call `assert_preflight_result` with the required checks, observed probes, and expected status. For failed checks, the assertion requires typed errors with nonblank codes, messages, and suggested actions. Provide `expected_errors` to verify classification and `synthetic_secrets` with captured logs to check redaction. Model typing establishes category, audience, and retryability types. Human review still establishes whether an action is useful and appropriate to its audience.

`hung_probe`, `cancellation_cleanup`, and `budget_retry` additionally require `assert_probe_lifetime` using measured elapsed time and independent evidence that background work stopped. Test recovered transients against extraction's actual retry or fallback under the same injected failure. Do not infer safe continuation solely from a retryable error.

F017 tests call `assert_extraction_scheduled` with fetched Temporal history, the gate activity name, expected extraction count, and expected terminal state. Failed workflows also require the expected typed failure. Mocked `execute_activity` calls are insufficient. F018 tests decode the actual response or outcome and call `assert_preflight_exit` to validate its verdict, check list, and typed failure.

These assertions validate supplied observations. They cannot establish that a test actually called the production handler, measured a real timeout, or fetched genuine history. Review the adapters alongside the tests. Scenario registration and assertion execution are coverage guards, not an automatic implementation of source-specific tests.

Static analysis follows directly resolvable helpers and imported error inheritance. Dynamic dispatch, arbitrary client instances, custom construction, decorator wrappers, and recovery semantics can exceed its analysis. A clean static report is not runtime certification. PR #3685's target gate behavior must be verified on its actual implementation; this package does not change SDK gate behavior or invent a release floor for its migration warnings.
