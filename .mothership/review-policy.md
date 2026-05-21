# application-sdk — By-Design Patterns

These patterns are intentional. Do NOT flag them as issues.

## Accepted Patterns

### Known Tech Debt (Tracked)
- `common/utils.py` contains mixed utilities — known dumping ground, tracked
  in BLDX milestone. Only flag SECURITY issues in this file, not structural.

### Infrastructure Abstraction Layers
- `execution/_temporal/` uses direct `temporalio` imports — this IS the
  abstraction layer. Not a violation.
- `infrastructure/_dapr/` uses direct `dapr` imports — this IS the
  abstraction layer. Not a violation.
- `infrastructure/_redis/` uses direct `redis` imports — this IS the
  abstraction layer. Not a violation.

### Performance Patterns
- `ThreadPoolExecutor` per query in low-frequency paths (< 10 calls per
  workflow execution) is acceptable. Only flag if the path is hot (100+
  calls per execution) or if the task has heartbeat enabled.
- `run_in_thread` uses the default executor for non-heartbeat tasks. Only
  flag if the task has `heartbeat_timeout_seconds` set AND the blocking
  call is long-running (> 5s).

### Test Patterns
- Tests in `tests/unit/` use `asyncio_mode="auto"` — no `@pytest.mark.asyncio`
  needed. Do not flag its absence.
- `clean_app_registry` fixture is defined in `conftest.py` — tests that
  define App subclasses should use it, but it's not always necessary if
  the test doesn't register apps.

### Credential Handling
- `CredentialRef` objects in contracts are serializable references (no
  actual secrets). They are safe to pass through Temporal payloads.
- `CredentialResolver` is intentionally restricted to `@task` methods
  because resolution requires I/O to the secret store.

### Error Codes
- `AAF-{COMPONENT}-{ID:03d}` format is intentional. Do not suggest
  alternative error code schemes.
- `NonRetryableError` and `RetryableError` are the two error base classes.
  Do not suggest additional error hierarchies.
