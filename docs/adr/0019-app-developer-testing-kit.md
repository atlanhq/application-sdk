# ADR-0019: App-Developer Testing Kit

## Status
**Accepted**

## Context

ADR-0005 promised "testability transformation": because app code only touches infrastructure through `self.context` and Protocol-based interfaces, the same `@task` bodies that run as Temporal activities in production should be executable in a plain unit test with in-memory fakes. The fakes exist (`MockStateStore`, `MockSecretStore`, `MockCredentialStore`, `MockHeartbeatController`, obstore `MemoryStore`/`LocalStore`), but composing them was left to each app team.

In practice that composition is non-trivial: a working test needs an `AppContext` wired with the fakes, the module-level infrastructure context set (so storage helpers called without an explicit store resolve), a `TaskExecutionContext` installed around each task invocation (so heartbeats and `run_in_thread` work), and teardown that restores global state. The evidence that this bar is too high:

- **5 of 5 partner apps failed their initial review** on test quality — each had either no tests for `@task` logic, or tests that mocked the SDK itself instead of using the fakes.
- Apps cannot cheaply test `@task` logic without standing up Dapr and Temporal, so most don't; their first real execution environment is a tenant.
- **SDK upgrades regress apps silently.** In a June 2026 incident, an error-message wording change in the SDK broke error-tolerance logic in multiple apps at once — none of them had tests that would have caught the behavior change before rollout, and the SDK had no integration tests pinning the contract those apps depended on.

The same audit found the v2→v3 objectstore migration paths (a recent prod-fix area) had no integration coverage at all: path-normalization parity between v2-style `./local/tmp/...` keys and v3 store keys, typed error branches, and `FileReference` round trips were only exercised indirectly through unit-level mocks.

## Decision

Two complementary pieces:

1. **A one-call scenario factory** — `application_sdk.testing.AppTestHarness` — that composes the *existing* in-memory infrastructure fakes around a live App instance. It mirrors production task wrapping (each `@task` invocation gets its own `TaskExecutionContext`) but executes inline, scopes the module-level infrastructure context to the `with` block, and captures task outputs, state writes, secret reads, stored objects, and heartbeats for assertion. App developers write:

   ```python
   with AppTestHarness(MyApp, secrets={"api-token": "t"}) as harness:
       out = await harness.execute("run", MyInput(...))
       assert harness.outputs[0].message == "extracted"
   ```

2. **A storage integration matrix in SDK CI** (`tests/integration/storage/test_localfs_*.py`, `integration` marker, no cloud credentials or Temporal required) that pins the v2→v3 objectstore contract against a real local obstore: round trips including the chunked >16 MiB path, prefix batch operations, v2-style staging-path vs v3 key parity and cross-shape interoperability, typed error branches, empty-prefix behavior, and `FileReference` persist/materialize round trips. SDK changes that would silently alter behavior apps depend on now fail in SDK CI, not in app review or production.

## Options Considered

### Option 1: One-call scenario factory + storage integration matrix (Chosen)

**Pros:**
- **Zero infrastructure**: tests run in-process; CI for apps needs only `pytest`
- **Reuses the ADR-0005 fakes**: no second mock ecosystem to keep in sync
- **Execution parity**: the same task bodies run inline that run as activities in production; task-context semantics (e.g. `task_context` unavailable in `run()`) are preserved
- **Captured evidence**: outputs, state, secrets, storage, and heartbeats are observable without hand-rolled spies
- **Contract pinned in SDK CI**: the storage matrix guards the v2→v3 migration surface where production fixes have landed recently

**Cons:**
- **Not a durability test**: retries, timeouts, heartbeat-driven restarts, and payload serialization are not exercised — those still need the Temporal-backed integration tests
- One more public surface for the SDK team to maintain

### Option 2: Per-app bespoke fixtures (Status quo — Rejected)

Each app hand-assembles fake context + state + objectstore.

**Cons:**
- Demonstrably not happening: 5/5 partner apps failed review
- Every app re-discovers the same wiring pitfalls (global infrastructure context leaking between tests, missing task context for heartbeats)
- Fixture drift: when the SDK changes context wiring, every app's bespoke fixture breaks differently

### Option 3: Full docker-compose harness (Rejected for unit-level)

Ship a compose file that stands up Dapr + Temporal and run app tests against real infrastructure.

**Cons:**
- Heavyweight: minutes of startup for millisecond-scale logic tests; hostile to TDD loops and laptop development
- Flaky in shared CI runners (ports, sidecar readiness, image pulls)
- Doesn't address the actual failure mode — apps weren't avoiding tests because Temporal was *unavailable*, but because the setup cost exceeded the perceived value

This remains the right tool for end-to-end workflow tests (`tests/integration/` with the `integration` marker already covers the SDK side), just not the entry-level testing story.

## Rationale

1. **Meet developers where the failure is**: the cheapest possible path from "wrote a task" to "tested the task" is one context manager and one `await`.
2. **ADR-0005 already paid for this**: the abstraction boundary makes inline execution faithful; the harness only packages it.
3. **Regression direction matters**: app-breaking SDK changes should fail in SDK CI (storage matrix) before they can reach apps; app logic changes should fail in app CI (harness) before they reach tenants.
4. **Minimal surface**: one class, no new fake implementations — the harness composes what already exists, so it cannot drift from the fakes apps also use directly.

## Consequences

**Positive:**
- App developers can test `@task` logic end-to-end in-process, including state, secrets, credential resolution, storage, and heartbeats
- The v2→v3 objectstore contract (path normalization, typed errors, empty-prefix behavior, `FileReference` lifecycle) is pinned by SDK CI on every commit
- Review can require harness-based tests from partner apps with a concrete, documented pattern to point at
- SDK behavior changes that apps depend on surface as SDK test failures with a clear contract to consult

**Negative:**
- The harness is not a substitute for durability testing; apps still need at least smoke-level workflow tests against Temporal before release
- Inline execution cannot catch payload-serialization or determinism bugs (those require the Temporal sandbox); the existing `tests/integration/` patterns remain necessary
- The SDK team owns one more public API whose semantics must track production task wrapping
