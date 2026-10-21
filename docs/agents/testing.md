# Testing

- Test command and conventions: `docs/standards/testing.md`.
- New code should target 85% coverage per `docs/standards/review-checklist.md` (the tooling threshold in `pyproject.toml` is 85% — CI fails below it).
- Current tests live in `tests/unit/`; follow existing structure when adding new tests.
- For how a *consumer app's* tests should be laid out, read `docs/agents/canonical-apps.md` and then the app itself — not an arbitrary `atlan-*-app`, which may be mid-migration or carry deprecated patterns.
- When a fixture stands in for a **generated artifact** (`app/generated/**`, `atlan.yaml`, a served API envelope), build it from a real one and say in a comment which repo it was read from. A fixture with the shape inverted still passes — for a reason unrelated to its claim — and it flatters the code under test, which is APP-TESTS-001's self-certifying PR arriving through the fixture rather than the assertion. FND-1667 shipped one: a credential template written as `{"id": ...}` with no `config`, where the toolkit emits `config` and no `id`. Nothing caught it until someone ran the selector against a real tree.
- For consumer apps built on this SDK, the conformance suite's T-series (`packages/conformance/conformance/docs/rules/tests.md`) enforces the agreed per-connector testing-tier architecture (unit + integration required, e2e recommended, UI optional except for top connectors) plus test-quality checks — assertion-free tests, uncollectable test files, disabled coverage gates, and more. Run it with `/remediate` or `uv run atlan-application-sdk-conformance detect --series T`.

## Shared State Under xdist

An xdist worker runs many tests in one process, and threads from earlier tests
(offloads, log flushes) are still running. Keep every test's changes local:

- **Never patch a stdlib function through an SDK module path.**
  `patch("application_sdk.X.time.monotonic")` patches the one global `time`
  module, so the event loop and every thread see the fake. Use
  `tests.unit.private_stdlib.private_stdlib(X, "time")` and patch the copy it
  yields. `tests/unit/test_no_global_stdlib_patches.py` fails the global forms.
- **Never `importlib.reload` an SDK module.** A reload makes new class and enum
  objects that earlier importers do not see. Put import-time env parsing in a
  `_load_*()` function and test that.
- Use `monkeypatch.setenv` / `delenv` / `chdir` / `syspath_prepend`, not
  `os.environ` / `os.chdir` / `sys.path` directly.
- `tests/unit/conftest.py` already resets, around every unit test: the App and
  Task registries, the infrastructure context, the sizing sink, psycopg2's
  encodings map, and `os.environ`. It also loads the hypothesis `unit` profile
  (`deadline=None`); do not call `settings.load_profile` in a test module.

## Timeouts a Passing Test Must Beat

A healthy test must never have to *finish* inside a timeout that exists to prove
the *failure* path. On a loaded 4-vCPU runner — Windows especially, where
`Event.wait(0.001)` rounds to ~15.6 ms and thread-pool spin-up is slow — the
success path misses a short bar and reads as a TIMEOUT, an unverifiable source,
or a hang.

- Any timeout a **passing** test must beat is **≥5s**; **≥30s** when it covers
  subprocess or pool spin-up. This includes `pytest.mark.timeout`, the bound you
  give `asyncio.wait_for`, and every budget or deadline handed to the code under
  test.
- Short timeouts are only for tests whose subject *is* the timeout, and only on
  the step that hangs: `test_source_probes.py`'s `_cut_off("tcp")` keeps DNS and
  TLS generous while cutting TCP short.
- `pytest.mark.timeout` counts fixture setup too, and on Windows its thread
  method calls `os._exit`, killing the whole xdist worker — a "worker crashed"
  red with no FAILED line. Use it only as a hang guard (`timeout(30)`).
- Prove "did not wait" or "did not block the loop" structurally, not with a
  wall-clock upper bound: record the budget at the seam that would wait
  (`test_restart_marker.py`'s `idle` fixture), or assert that a blocked call has
  not finished when the loop observes it (`test_sql_client_cancellation.py`).
  A lower bound (`elapsed >= budget`) is safe; an upper bound measures runner
  load.

## Asserting a Preflight Gate Verdict

The gate emits one `Preflight gate outcome` row per invocation and picks the log
level from the verdict (FND-901): `error` for a block or an unverifiable source,
`warning` when it proceeded with a failed advisory check, `info` otherwise. Read
the row through the shared capture rather than patching
`preflight_gate.logger` by hand — a reader that watches `info` alone returns an
empty list on exactly the runs it was written to pin, and keeps passing:

```python
from application_sdk.testing import capture_preflight_outcomes  # in conftest.py


async def test_block_names_the_failing_check(capture_preflight_outcomes):
    ...
    assert capture_preflight_outcomes.level == "error"
    assert capture_preflight_outcomes.matrix[0]["name"] == "credentialScopes"
```

Suites that patch the logger with a `MagicMock` themselves can use
`outcome_rows` / `outcome_level` / `single_outcome` over the mock instead. Both
paths assert *exactly one* row, because returning the first match hides a double
emission.

Boundary: the capture covers rows the gate **activity** emits. The workflow
wrapper's `skipped` / `no_verdict` rows go through Temporal's `workflow.logger`
(`app/base.py`), which the capture does not patch — pin those paths against
`workflow.logger` directly.

## What SDK Review Checks for Tests

The reviewer enforces these test rules (G4 guardrail):

- New public API (class/method/module exported in `__init__.py`) -> MUST have tests
- Bug fix PR -> MUST have a regression test
- Security fix PR -> MUST have a regression test
- Performance fix PR -> MUST have a behavior test
- Code quality / docs / v2 cleanup -> existing tests passing is sufficient

### Test Patterns the Reviewer Flags

- `@pytest.mark.asyncio` (redundant with `asyncio_mode="auto"` — remove it)
- `assert result` or `assert result is not None` (too vague — assert specific values)
- Missing `clean_app_registry` fixture when defining App subclasses in tests
- Real external calls in unit tests (use `MockStateStore`, `MockSecretStore`, `MockPubSub`)
- Missing `MockHeartbeatController` for heartbeat-enabled `@task` methods
- Tests checking implementation details instead of behavior
