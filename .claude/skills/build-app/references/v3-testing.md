# V3 Testing — Quick Reference

## Test Stack

- `pytest` with `pytest-asyncio` (async test support)
- `pytest-cov` for coverage
- SDK provides mock infrastructure — no Dapr sidecar or Temporal server needed for unit tests

## SDK Test Fixtures

Import from `application_sdk.testing.fixtures`:

```python
# In conftest.py or test files
from application_sdk.testing.fixtures import (
    app_context,           # AppContext wired with mock state + secrets
    mock_state_store,      # Fresh MockStateStore
    mock_secret_store,     # Fresh MockSecretStore
    mock_pubsub,           # Fresh MockPubSub
    mock_binding,          # Fresh MockBinding
    mock_heartbeat,        # Fresh MockHeartbeatController
    mock_credential_store, # Fresh MockCredentialStore
    clean_app_registry,    # Reset AppRegistry between tests
    clean_task_registry,   # Reset TaskRegistry between tests
)
```

## Mock Implementations

From `application_sdk.testing.mocks`:

```python
from application_sdk.testing.mocks import (
    MockStateStore,        # Dict-backed state store
    MockSecretStore,       # Dict-backed secret store
    MockBinding,           # In-memory binding
    MockPubSub,            # In-memory pub/sub
    MockCredentialStore,   # Credential testing
    MockHeartbeatController, # Track heartbeat calls
)
```

### Pre-loading mock data:

```python
@pytest.fixture
def secret_store():
    store = MockSecretStore()
    store.secrets = {"api-key": "test-secret-value"}
    return store

@pytest.fixture
def state_store():
    store = MockStateStore()
    store.state = {"checkpoint": {"offset": 0}}
    return store
```

## conftest.py Template

```python
"""Shared test fixtures."""

import pytest
from application_sdk.app.context import AppContext
from application_sdk.testing.mocks import MockSecretStore, MockStateStore


@pytest.fixture
def mock_state_store():
    return MockStateStore()


@pytest.fixture
def mock_secret_store():
    store = MockSecretStore()
    # Pre-load test secrets matching your handler's expected credentials
    store.secrets = {
        "test-api-key": "fake-key-for-testing",
    }
    return store


@pytest.fixture
def app_context(mock_state_store, mock_secret_store):
    return AppContext(
        app_name="test-app",
        app_version="0.0.1",
        _state_store=mock_state_store,
        _secret_store=mock_secret_store,
    )
```

## Testing Contracts

```python
def test_input_has_defaults():
    """Input can be created with just defaults."""
    inp = MyAppInput()
    assert inp.workflow_id == ""

def test_input_rejects_bad_type():
    """Input rejects wrong types."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        MyAppInput(batch_size="not-an-int")

def test_output_serializes():
    """Output can round-trip through JSON."""
    out = MyAppOutput(records_processed=42)
    json_str = out.model_dump_json()
    restored = MyAppOutput.model_validate_json(json_str)
    assert restored.records_processed == 42
```

## Testing Handler Methods

```python
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus, HandlerCredential,
)

class TestMyHandler:
    @pytest.fixture
    def handler(self):
        return MyHandler()

    @pytest.fixture
    def credentials(self):
        return [
            HandlerCredential(key="host", value="test.example.com"),
            HandlerCredential(key="api_key", value="test-key"),
        ]

    @pytest.mark.asyncio
    async def test_auth_success(self, handler, credentials):
        # Mock the external client if needed
        result = await handler.test_auth(AuthInput(credentials=credentials))
        assert isinstance(result, AuthOutput)
        assert result.status == AuthStatus.SUCCESS
```

## Testing @task Methods

Task methods need infrastructure mocks since they access `self.context`:

```python
from unittest.mock import AsyncMock, patch

class TestMyAppTasks:
    @pytest.mark.asyncio
    async def test_fetch_task(self, app_context):
        app = MyApp()
        app._context = app_context  # Inject mock context

        # Mock external API calls
        with patch("app.my_app.MyClient") as mock_client:
            mock_client.return_value.fetch.return_value = [{"id": 1}]

            result = await app.fetch_data.__wrapped__(
                app, FetchInput(source_id="test")
            )

            assert result.record_count == 1
```

Note: `__wrapped__` accesses the original async function before the `@task` decorator wraps it for Temporal. This is needed for unit testing outside of a Temporal worker.

## pytest Configuration

In `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

## Running Tests

```bash
# All tests
uv run pytest tests/ -v

# Unit tests only
uv run pytest tests/unit/ -v

# With coverage
uv run pytest tests/ --cov=app --cov-report=term-missing

# Single test
uv run pytest tests/unit/test_handler.py::TestMyHandler::test_auth_success -v
```
