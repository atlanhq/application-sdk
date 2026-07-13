# Testing

- Test command and conventions: `docs/standards/testing.md`.
- New code should target 85% coverage per `docs/standards/review-checklist.md` (the tooling threshold in `pyproject.toml` is 85% — CI fails below it).
- Current tests live in `tests/unit/`; follow existing structure when adding new tests.
- Mutation testing (advisory pilot): the `mutation-tests.yaml` workflow runs mutmut on the Python files a PR changes and posts a scorecard of seeded faults the unit suite missed. It never blocks the PR. Run locally with `uv run poe mutation-diff`; inspect a surviving mutant with `uv run --group mutation mutmut show <mutant-name>`.
- For consumer apps built on this SDK, the conformance suite's T-series (`packages/conformance/conformance/docs/rules/tests.md`) enforces the agreed per-connector testing-tier architecture (unit + integration required, e2e recommended, UI optional except for top connectors) plus test-quality checks — assertion-free tests, uncollectable test files, disabled coverage gates, and more. Run it with `/remediate` or `uv run atlan-application-sdk-conformance detect --series T`.

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
