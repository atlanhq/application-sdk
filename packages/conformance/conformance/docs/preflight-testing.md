# Preflight conformance

P052–P061 and P065 add static diagnostics for handler types, actionable errors, raised expected failures, verdict consistency, entrypoint inputs, blocking probes, budgets, cleanup, sensitive failure text, removed gate configuration, and incomplete analysis. Existing P032–P035 and P047 continue to apply. The new rules initially warn.

Run static checks:

```bash
uv run atlan-application-sdk-conformance detect --repo . --series P --static --output preflight.sarif
```

Run registered behavior scenarios in the app's existing test environment:

```bash
uv run atlan-application-sdk-conformance detect --repo . --rule P062 --with-tests --test-python .venv/bin/python --test-timeout 120 --output preflight.sarif
```

Use `--rule P063,P064 --scope sdk --with-tests` for SDK scenarios. Install the package's existing `test` extra in the test environment. Static use does not require pytest. Test execution is opt-in because it imports and executes repository tests, including their fixtures. Use synthetic source adapters and isolated infrastructure.

Mark tests with `pytest.mark.preflight_conformance(rule="P062", scenario="healthy", entrypoint="crawler")`. The authoritative scenario names are in `conformance.preflight_testing.SCENARIOS`. Each selected entrypoint needs the complete applicable matrix. Unregistered, skipped, unsupported, failed, timed-out, and unexpectedly passing xfail scenarios do not establish conformance. A missing matrix emits findings; a static run records `not_evaluated` in SARIF `runs[].properties["atlan/preflightTests"]`.

P062 tests invoke the real handler using an app-owned source adapter and call `assert_preflight_result` with the required checks, observed probes, and expected status. For failed checks, the assertion requires typed errors with nonblank codes, messages, and suggested actions. Provide `expected_errors` to verify classification and `synthetic_secrets` with captured logs to check redaction. Model typing establishes category, audience, and retryability types. Human review still establishes whether an action is useful and appropriate to its audience.

`hung_probe`, `cancellation_cleanup`, and `budget_retry` additionally require `assert_probe_lifetime` using measured elapsed time and independent evidence that background work stopped. Test recovered transients against extraction's actual retry or fallback under the same injected failure. Do not infer safe continuation solely from a retryable error.

P063 tests call `assert_extraction_scheduled` with fetched Temporal history, the gate activity name, expected extraction count, and expected terminal state. Failed workflows also require the expected typed failure. Mocked `execute_activity` calls are insufficient. P064 tests decode the actual response or outcome and call `assert_preflight_exit` to validate its verdict, check list, and typed failure.

These assertions validate supplied observations. They cannot establish that a test actually called the production handler, measured a real timeout, or fetched genuine history. Review the adapters alongside the tests. Scenario registration and assertion execution are coverage guards, not an automatic implementation of source-specific tests.

Static analysis follows directly resolvable helpers and imported error inheritance. Dynamic dispatch, arbitrary client instances, custom construction, decorator wrappers, and recovery semantics can exceed its analysis. A clean static report is not runtime certification. PR #3685's target gate behavior must be verified on its actual implementation; this package does not change SDK gate behavior or invent a release floor for its migration warnings.
