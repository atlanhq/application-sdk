# Testing

- Test command and conventions: `docs/standards/testing.md`.
- New code should target 85% coverage per `docs/standards/review-checklist.md` (the tooling threshold in `pyproject.toml` is 85% — CI fails below it).
- Current tests live in `tests/unit/`; follow existing structure when adding new tests.
- For how a *consumer app's* tests should be laid out, read `docs/agents/canonical-apps.md` and then the app itself — not an arbitrary `atlan-*-app`, which may be mid-migration or carry deprecated patterns.
- For consumer apps built on this SDK, the conformance suite's T-series (`packages/conformance/conformance/docs/rules/tests.md`) enforces the agreed per-connector testing-tier architecture (unit + integration required, e2e recommended, UI optional except for top connectors) plus test-quality checks — assertion-free tests, uncollectable test files, disabled coverage gates, and more. Run it with `/remediate` or `uv run atlan-application-sdk-conformance detect --series T`.

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
