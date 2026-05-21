# SDK Review — Standard Mode

When reviewing application-sdk PRs in standard mode.

## What to Flag

### Temporal Determinism (BLOCKING — Guardrails G2, G3)
- `datetime.now()`, `datetime.utcnow()`, `time.time()` in `run()`/`@entrypoint`
- `uuid.uuid4()`, `uuid.uuid1()` in `run()`/`@entrypoint`
- `random.*` calls in `run()`/`@entrypoint`
- Direct I/O (network, file, database) in `run()`/`@entrypoint`
- Any non-deterministic operation in workflow context

### Contract Safety (BLOCKING — Guardrail G2)
- Field removed from Input/Output contract
- Field renamed on Input/Output contract
- Field type changed on Input/Output contract
- `list = []` or `dict = {}` as default (use `Field(default_factory=...)`)
- New required field without default (breaks existing callers)

### Credential Safety (BLOCKING — Guardrail G5)
- Secrets, tokens, API keys in log statements
- Raw credential values in Input/Output contracts (use `CredentialRef`)
- `CredentialResolver` used outside `@task` methods

### Infrastructure Abstraction (HIGH)
- Direct `temporalio` import outside `execution/_temporal/`
- Direct `dapr` import outside `infrastructure/_dapr/`
- Direct `redis` import outside `infrastructure/_redis/`
- Reverse dependency: `infrastructure/` importing from `app/`

### API Surface (CRITICAL — Guardrails G4, G6)
- New public API (in `__init__.py`) without corresponding test
- Public export removed without `warnings.warn()` deprecation shim
- Public method signature changed without backward compatibility

### Blocking Operations (HIGH)
- Synchronous HTTP calls (`requests.get`, `urllib`) in async `@task` without `run_in_thread()`
- Blocking file I/O in `@task` without `run_in_thread()`
- `time.sleep()` in async context

### Code Quality (MEDIUM)
- f-strings in log calls (use `logger.info("msg %s", val)`)
- `print()` in production code
- Bare `except:` clause
- Exception caught but not re-raised or logged
- Missing `from e` in exception chaining
- Function > 50 lines, class > 300 lines, nesting > 3 levels

### Test Quality (varies)
- Missing tests for new `@task` methods (CRITICAL)
- `@pytest.mark.asyncio` decorator (redundant — remove it)
- Vague assertions (`assert result`, `assert result is not None`)
- Missing `clean_app_registry` fixture when test defines App subclasses
- Real external calls in unit tests (use SDK mocks)

### v2 Patterns (HIGH)
- `from application_sdk.workflows` / `.activities` / `.handlers`
- `@workflow.defn` / `@activity.defn` decorators
- `ActivitiesInterface` / `WorkflowInterface` / `HandlerInterface` subclasses
- `ObjectStore` from `application_sdk.services`
- `@dataclass` on Input/Output subclasses

## What NOT to Flag

- Import ordering (isort with custom config handles this)
- Missing docstrings on private methods (prefixed with `_`)
- Pre-existing code outside the diff
- Style preferences (single vs double quotes, etc.)
- `Dict[str, Any]` in infrastructure layer (allowed for Dapr interop)
- `print()` in test files
- `asyncio_mode="auto"` setup (it's correct, no decorator needed)
- Unused imports (ruff handles this)
- Deprecated dependencies (Dependabot handles)

## Severity Calibration

- **BLOCKING/CRITICAL**: Use `pattern_id` from `severity-rubric.yaml` when applicable
- **HIGH**: Issues users will hit in common flows (inline comment REQUIRED)
- **MEDIUM**: Issues users rarely hit (body only)
- **LOW**: Defensive notes (body only)
- **INFO**: Pattern observation (body only)

Confidence floors:
- BLOCKING/CRITICAL: >= 0.85
- HIGH: >= 0.80
- MEDIUM: >= 0.55
- LOW: >= 0.40
