# Testing

- Test command and conventions: `docs/standards/testing.md`.
- New code should target 85% coverage per `docs/standards/review-checklist.md` (the tooling threshold in `pyproject.toml` is 85% — CI fails below it).
- Current tests live in `tests/unit/`; follow existing structure when adding new tests.
- For how a *consumer app's* tests should be laid out, read `docs/agents/canonical-apps.md` and then the app itself — not an arbitrary `atlan-*-app`, which may be mid-migration or carry deprecated patterns.
- When a fixture stands in for a **generated artifact** (`app/generated/**`, `atlan.yaml`, a served API envelope), build it from a real one and say in a comment which repo it was read from. A fixture with the shape inverted still passes — for a reason unrelated to its claim — and it flatters the code under test, which is APP-TESTS-001's self-certifying PR arriving through the fixture rather than the assertion. FND-1667 shipped one: a credential template written as `{"id": ...}` with no `config`, where the toolkit emits `config` and no `id`. Nothing caught it until someone ran the selector against a real tree.
- For consumer apps built on this SDK, the conformance suite's T-series (`packages/conformance/conformance/docs/rules/tests.md`) enforces the agreed per-connector testing-tier architecture (unit + integration required, e2e recommended, UI optional except for top connectors) plus test-quality checks — assertion-free tests, uncollectable test files, disabled coverage gates, and more. Run it with `/remediate` or `uv run atlan-application-sdk-conformance detect --series T`.

## Never Assert on Elapsed Wall-Clock Time

`assert elapsed < 1.0`, `assert lag < 0.1`, `assert ticks >= n` measure the CI
runner, not the code: a loaded 4-vCPU runner misses the bar with nothing wrong
and one matrix leg goes red at random (FND-4233). Assert the property instead:

- **The event loop stayed free** — route the blocking cost through
  `tests/unit/loop_probe.py::LoopProbe.block()` and call
  `assert_loop_stayed_free`.
- **A budget or remaining time** — inject the clock (`clock=`, a module-local
  `_monotonic` seam) or freeze the one the code reads; assert the exact value.
- **It returned promptly / did not wait for a blocked call** — have the blocked
  call set a flag only when it returns and assert the flag is unset once the
  caller has returned; or bound the wait with `asyncio.wait_for` / `Event.wait`
  at 5–30s, so the bound decides how long a regression takes to fail, never
  whether a healthy run passes.
- **A complexity guard** (linear, not quadratic) — count the steps; failing
  that, size the input so the regression takes minutes and bound it at 30s.
  A ratio of two millisecond timings swings past any bar under load, even
  fastest-of-five.

`tests/unit/test_no_wall_clock_asserts.py` fails on any new ordered assertion
over a clock delta or a value named `elapsed`/`lag`/`ticks`/`gap(s)`/`took`.

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
