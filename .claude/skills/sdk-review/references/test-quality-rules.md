# v3 Test Quality Review Rules

Test patterns, coverage requirements, and quality standards for application-sdk v3.

---

## Test Existence (Critical)

### Every New Module Needs Tests

If the PR adds or significantly modifies a module in `application_sdk/`, there must be corresponding test files in `tests/`.

**Mapping convention:**
```
application_sdk/app/base.py        → tests/unit/app/test_base.py
application_sdk/contracts/types.py → tests/unit/contracts/test_types.py
application_sdk/storage/ops.py     → tests/unit/storage/test_ops.py
application_sdk/handler/service.py → tests/unit/handler/test_service.py
```

Flag as **Critical** if:
- New public API added without any tests
- New `@task` method without a test exercising it
- New `Handler` method without a test
- New contract class without validation tests

Flag as **Important** if:
- Existing module changed but no test updates (may indicate untested behavior change)
- New error handling paths without negative tests

---

## Test Infrastructure (v3 Patterns)

### Mock Infrastructure for Unit Tests (Critical)

Unit tests must NOT require Dapr or Temporal sidecars. Use mock implementations from `application_sdk.testing.mocks`:

```python
# GOOD — mock implementations, no sidecar needed
from application_sdk.testing import MockStateStore, MockSecretStore, MockPubSub

@pytest.fixture
def infra():
    return InfrastructureContext(
        state_store=MockStateStore(),
        secret_store=MockSecretStore({"api-key": "test-secret"}),
        pub_sub=MockPubSub(),
    )
```

> **Important:** These mocks are for tests only. Production and local-dev runtime requires the Dapr sidecar — `DaprStateStore`/`DaprSecretStore` are the only runtime implementations. There are no InMemory fallback implementations.

Flag:
- Tests that import from `dapr.clients` directly
- Tests that require `DAPR_HTTP_PORT` or `DAPR_GRPC_PORT` environment variables
- Tests that connect to real Temporal server (unless marked `@pytest.mark.integration`)
- Tests using `unittest.mock.patch` on infrastructure when `MockStateStore`/`MockSecretStore` exists
- Production/app code that conditionally falls back to any non-Dapr implementation at runtime

### clean_app_registry Fixture (Critical)

`App.__init_subclass__` registers every subclass in `AppRegistry` at import time. Tests that define `App` subclasses MUST use the cleanup fixture to prevent cross-test pollution:

```python
# conftest.py — required
from application_sdk.testing.fixtures import clean_app_registry  # noqa: F401

# Or per-test
@pytest.fixture(autouse=True)
def clean_registries(clean_app_registry):
    pass
```

Flag:
- Test files defining `App` subclasses without `clean_app_registry` in their conftest
- Test files defining `App` subclasses that import `clean_app_registry` but don't use it as autouse

### Async Test Pattern (Critical)

pytest config uses `asyncio_mode = "auto"`. All async tests must be plain `async def`:

```python
# GOOD
async def test_fetch_data():
    result = await app.fetch(FetchInput(source="test"))
    assert result.count > 0

# BAD — unnecessary decorator with asyncio_mode=auto
@pytest.mark.asyncio
async def test_fetch_data():
    ...
```

Flag:
- `@pytest.mark.asyncio` decorator when `asyncio_mode = "auto"` is set (redundant)
- Sync test functions that `asyncio.run()` inside them (use native async)

---

## Test Quality

### Specific Assertions (Important)

```python
# BAD — too vague
assert result
assert result is not None
assert len(output.records) > 0

# GOOD — specific
assert result.status == "completed"
assert result.record_count == 42
assert len(output.records) == 3
assert output.records[0].name == "expected_name"
```

### Test Behavior, Not Implementation (Important)

```python
# BAD — testing implementation details
mock_state_store.get.assert_called_once_with("key-123")
assert app._internal_cache == {"key": "value"}

# GOOD — testing observable behavior
result = await app.process(ProcessInput(key="123"))
assert result.status == "processed"
assert result.output_path.endswith(".parquet")
```

### Edge Cases and Error Paths (Important)

For each new feature, check for tests covering:
- Empty input (empty list, empty string, None where Optional)
- Boundary values (0, max int, very long strings)
- Invalid input (wrong types caught by Pydantic, missing required fields)
- Expected exceptions (using `pytest.raises`)
- Timeout behavior
- Retry behavior (if `retry_max_attempts` is configured)

```python
# GOOD — testing error path
async def test_fetch_with_invalid_credentials():
    with pytest.raises(CredentialError, match="Invalid API key"):
        await app.fetch(FetchInput(credential_ref=bad_ref))

# GOOD — testing empty input
async def test_process_empty_records():
    result = await app.process(ProcessInput(records=[]))
    assert result.count == 0
    assert result.status == "completed"
```

### Test Isolation (Critical)

Each test must be independent. Flag:
- Tests that depend on execution order
- Tests that share mutable module-level state
- Tests that write to the real filesystem without `tmp_path` fixture
- Tests that read environment variables without `monkeypatch`
- Tests that import or depend on other test files' fixtures without proper conftest

```python
# BAD — shared state
_counter = 0

def test_first():
    global _counter
    _counter += 1
    assert _counter == 1

def test_second():
    assert _counter == 1  # depends on test_first running first

# GOOD — isolated
def test_counter():
    counter = Counter()
    counter.increment()
    assert counter.value == 1
```

### No Real External Calls in Unit Tests (Critical)

Unit tests must not make real HTTP requests, database connections, or cloud storage calls. Flag:
- `httpx.AsyncClient()` without mocking in unit tests
- `boto3.client()` or `obstore` calls to real endpoints in unit tests
- Any network call not behind a mock/fixture

Integration tests (marked `@pytest.mark.integration`) are exempt.

---

## Contract Testing

### Payload Safety (Critical)

New `Input`/`Output` subclasses must be validated for payload safety:

```python
# GOOD — test that contract passes import-time validation
def test_my_input_is_payload_safe():
    # If this class exists without error, it passed validation
    input = MyInput(field="value", count=10)
    assert input.field == "value"

# GOOD — test bounded collections
def test_bounded_list_enforcement():
    with pytest.raises(ValidationError):
        MyInput(records=[{} for _ in range(10001)])  # exceeds MaxItems
```

### Serialization Round-Trip (Important)

Contracts that cross Temporal boundaries should have round-trip tests:

```python
def test_input_serializes_roundtrip():
    original = FetchInput(source="test", batch_size=50)
    serialized = original.model_dump_json()
    restored = FetchInput.model_validate_json(serialized)
    assert restored == original
```

### Evolution Safety (Important)

When a contract is modified, verify old serialized data still deserializes:

```python
def test_input_backwards_compatible():
    # Simulates payload from before the new field was added
    old_payload = '{"source": "test"}'
    input = FetchInput.model_validate_json(old_payload)
    assert input.source == "test"
    assert input.batch_size == 100  # new field has default
```

---

## Test Markers

### Correct Marker Usage (Important)

```python
# Unit tests — no marker needed (default)
async def test_unit_thing():
    ...

# Integration tests — requires @pytest.mark.integration
@pytest.mark.integration
async def test_with_real_temporal():
    ...

# E2E tests — requires @pytest.mark.e2e
@pytest.mark.e2e
async def test_full_workflow():
    ...
```

Flag:
- Integration/e2e tests without proper markers (they'd run in normal pytest and fail)
- Unit tests with `@pytest.mark.integration` marker (they should run without infrastructure)

---

## Coverage

### Minimum Threshold

Coverage minimum is 50% (`fail_under = 50` in pyproject.toml). Flag PRs that would drop coverage below this.

### Meaningful Coverage

Coverage percentage alone is insufficient. Flag:
- Tests that execute code but don't assert anything meaningful
- Tests that only test the happy path of complex branching logic
- Missing tests for `except` blocks and error recovery paths

---

## MockHeartbeatController

For tasks with heartbeat logic:

```python
from application_sdk.testing import MockHeartbeatController

async def test_task_with_heartbeat():
    controller = MockHeartbeatController()
    # inject into task context
    # ... run task ...
    assert len(controller.recorded_heartbeats) > 0
    assert controller.recorded_heartbeats[-1].records_done == 42
```

Flag tasks with `heartbeat_timeout_seconds` or `auto_heartbeat_seconds` that have no tests exercising the heartbeat progress tracking.
