# Testing

- Test command and conventions: `docs/standards/testing.md`.
- New code should target 85% coverage per `docs/standards/review-checklist.md` (the tooling threshold in `pyproject.toml` is 50%).
- Current tests live in `tests/unit/`; follow existing structure when adding new tests.

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
