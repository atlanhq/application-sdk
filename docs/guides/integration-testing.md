# Integration Testing Guide

This guide explains how to write integration tests for an Atlan connector built on Apps-SDK v3.

## The recommended pattern: in-process workflow tests

**Use `application_sdk.dev.embedded_runtime` + an `AppExecutor` shim to run the full Temporal workflow in-process against mocked secret / state / storage.** This is what every v3 connector ([`atlan-openapi-app`](https://github.com/atlanhq/atlan-openapi-app/tree/main/tests/integration), [`atlan-mysql-app`](https://github.com/atlanhq/atlan-mysql-app/tree/main/tests/integration), [`atlan-metabase-app`](https://github.com/atlanhq/atlan-metabase-app/tree/main/tests/integration)) ships under `tests/integration/`, and what the `connector-integration-tests@main` composite action is wired to run on every PR.

Why this is the right pattern for v3:

- **It exercises what actually runs in production** — the same `@entrypoint` method, the same `@task` graph, the same Temporal workflow boundaries. Bugs that only show up when the workflow is dispatched through Temporal (serialization, sandbox restrictions, retry semantics) get caught here. HTTP-only tests miss them.
- **No external services to manage.** Temporal starts as an embedded dev server via `embedded_runtime()`; the secret / state / storage layers are `MockSecretStore` / `MockStateStore` / `LocalStore`. The composite action just runs `pytest tests/integration/` — no Dapr CLI, no Temporal CLI, no docker-compose required at the CI step.
- **It tests typed inputs and outputs.** You call `execute_app(YourApp, YourInput(...))` and assert on the typed output dataclass. The contract is what the workflow code actually consumes; refactor-safe.
- **You can read the artifacts.** The mocked `LocalStore` writes everything the workflow `self.upload`s to a tmp dir. Tests open the resulting JSONL files and assert on their contents — what the `transform_data` task actually produced, what `metabaseQuery` ended up stamped on each question, etc. HTTP scenario tests can only assert on the response body.

The older `BaseIntegrationTest` (HTTP scenario) framework is still shipped for the narrow case where you need to lock the literal request/response contract of the app server's HTTP endpoints — see [Legacy: HTTP scenario tests](#legacy-http-scenario-tests) at the bottom. **For new connectors, do not start there.** Use the recommended pattern below.

## Quick start (recommended pattern)

### Step 1: Wire the conftest

Copy the conftest from any v3 reference connector — they're nearly identical. Below is the canonical shape; the only per-connector knobs are the `_TASK_QUEUE`, the credential-store seed, and the App class import.

```python
# tests/integration/conftest.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import orjson
import pytest
import pytest_asyncio
from application_sdk.dev import embedded_runtime
from application_sdk.execution._temporal.backend import TemporalExecutorBackend
from application_sdk.execution._temporal.converter import create_data_converter_for_app
from application_sdk.execution._temporal.worker import create_worker
from application_sdk.infrastructure.context import (
    InfrastructureContext,
    set_infrastructure,
)
from application_sdk.observability.observability import AtlanObservability
from application_sdk.storage import create_local_store, create_memory_store
from application_sdk.testing.mocks import MockSecretStore, MockStateStore
from temporalio.client import Client

from app.connector import YourApp  # noqa: F401 — triggers App registration

# Pre-wire a memory store as the deployment objectstore so the periodic
# observability flush does not keep retrying and spamming warnings in tests.
AtlanObservability._deployment_store = create_memory_store()

_TASK_QUEUE = "your-app-queue"
_CREDENTIAL_KEY = "your-app"


class AppExecutor:
    """Compatibility shim wrapping TemporalExecutorBackend for integration tests."""

    def __init__(self, backend: TemporalExecutorBackend) -> None:
        self._backend = backend

    async def execute_app(self, app_cls, input_data, *, execution_id_prefix: str = ""):
        from application_sdk.app.context import AppContext
        from application_sdk.execution.retry import RetryPolicy

        app_name = getattr(app_cls, "_app_name", execution_id_prefix or "app")
        context = AppContext(
            app_name=app_name, app_version="0.0.0", run_id=execution_id_prefix or app_name
        )
        return await self._backend.execute(
            app_cls, input_data, context=context, retry_policy=RetryPolicy()
        )


@pytest.fixture(scope="session")
def store_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("sdk-store")


@pytest.fixture(scope="session")
def infrastructure(store_root: Path) -> InfrastructureContext:
    """Mock infrastructure — seeds your credentials from env vars."""
    secrets: dict[str, str] = {}
    if os.environ.get("E2E_YOURAPP_HOST"):
        secrets[_CREDENTIAL_KEY] = orjson.dumps({
            "host": os.environ["E2E_YOURAPP_HOST"],
            # ... rest of credential fields
        }).decode()
    ctx = InfrastructureContext(
        state_store=MockStateStore(),
        secret_store=MockSecretStore(secrets),
        storage=create_local_store(store_root),
    )
    set_infrastructure(ctx)
    return ctx


@pytest_asyncio.fixture(scope="session")
async def embedded_temporal():
    async with embedded_runtime(log_level="error") as rt:
        yield rt


@pytest_asyncio.fixture(scope="session")
async def temporal_client(embedded_temporal) -> Client:
    data_converter = create_data_converter_for_app(YourApp)
    return await Client.connect(embedded_temporal.host, data_converter=data_converter)


@pytest_asyncio.fixture(scope="session")
async def your_app_worker(temporal_client, infrastructure) -> Any:  # noqa: ARG001
    w = create_worker(temporal_client, task_queue=_TASK_QUEUE)
    async with w:
        yield


@pytest.fixture(scope="session")
def your_app_executor(temporal_client, your_app_worker) -> AppExecutor:  # noqa: ARG001
    backend = TemporalExecutorBackend(client=temporal_client, task_queue=_TASK_QUEUE)
    return AppExecutor(backend=backend)
```

### Step 2: Write a workflow test

One class per scenario (happy path, filters, error path, etc.). Each class shares **one workflow run** across all its assertions via a class-scoped fixture — the workflow runs once and you assert on the shared result.

```python
# tests/integration/test_workflow.py
from __future__ import annotations
from typing import TYPE_CHECKING, cast
from pathlib import Path

import pytest
from application_sdk.contracts.types import ConnectionRef
from application_sdk.credentials.ref import CredentialRef

from app.connector import YourApp
from app.contracts import YourInput, YourOutput

if TYPE_CHECKING:
    from tests.integration.conftest import AppExecutor


class TestExtractionWorkflow:
    @pytest.fixture(scope="class")
    async def extraction_result(self, your_app_executor: "AppExecutor", tmp_path_factory) -> YourOutput:
        output_dir = tmp_path_factory.mktemp("output")
        return cast(
            "YourOutput",
            await your_app_executor.execute_app(
                YourApp,
                YourInput(
                    your_credential=CredentialRef(name="your-app", credential_type="basic"),
                    connection=ConnectionRef.model_validate({...}),
                    output_path=str(output_dir),
                ),
            ),
        )

    @pytest.mark.asyncio
    async def test_total_records_positive(self, extraction_result: YourOutput) -> None:
        assert extraction_result.total_records > 0

    @pytest.mark.asyncio
    async def test_transformed_jsonl_contains_records(self, extraction_result: YourOutput) -> None:
        # Read the actual file the transform @task wrote — the strongest assertion.
        f = Path(extraction_result.output_path) / "transformed" / "YOURTYPE" / "result-0.json"
        records = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        assert len(records) > 0
        for r in records[:5]:
            assert r["typeName"] == "YourType"
            assert r["attributes"]["qualifiedName"]
```

### Step 3: Run

```bash
# Set env vars for the real source the workflow talks to.
export E2E_YOURAPP_HOST=https://your.source
export E2E_YOURAPP_USERNAME=...
export E2E_YOURAPP_PASSWORD=...

uv run pytest tests/integration/ -v
```

In CI: the `tests` job in your `.github/workflows/tests.yaml` uses [`connector-integration-tests@main`](../standards/connector-ci-e2e.md) which runs exactly this command, with the env vars wired from repo secrets.

## What to test

| Scenario | What to assert |
|---|---|
| Happy path | Output dataclass fields are populated; transformed JSONL files exist; records carry the typed attributes that downstream nodes (QI, publish) read. |
| Filters (`include_*` / `exclude_*`) | Workflow accepts the typed filter shape and completes. Filter logic itself is unit-tested elsewhere; integration test just verifies the contract threads through. |
| Inline credentials path | `credentials=[{"key":...}]` works as a fallback when `CredentialRef` is absent. Catches regressions in `build_credential_ref` and the per-task credential routing. |
| Second `@entrypoint` (if your App has one) | Smoke-test that it accepts its typed input and returns a well-formed output even on empty intermediate state (e.g. empty QI prefix). |
| Error paths | Misconfigured credentials surface as a typed `AppError` subclass, not a bare `Exception`. |

Refer to the connector adopters' suites for working examples — [`atlan-openapi-app/tests/integration/test_openapi.py`](https://github.com/atlanhq/atlan-openapi-app/blob/main/tests/integration/test_openapi.py), [`atlan-metabase-app/tests/integration/test_metabase_workflow.py`](https://github.com/atlanhq/atlan-metabase-app/blob/main/tests/integration/test_metabase_workflow.py).

---

## Legacy: HTTP scenario tests

> **Use the [recommended pattern](#the-recommended-pattern-in-process-workflow-tests) above for new connectors.** This section documents the older `BaseIntegrationTest` framework — kept for connectors that still need to lock the HTTP request/response contract of the app server (auth/preflight/metadata endpoints) for regression purposes.

The HTTP scenario framework provides a **declarative, data-driven** approach to testing the running app server's HTTP surface. Instead of writing procedural test code, you define **scenarios** that specify:

- What API to test
- What inputs to provide
- What outputs to expect

The framework handles the rest: calling APIs, validating assertions, and reporting results.

### When to use the legacy framework

Most v3 connectors do **not** need it — the in-process workflow tests above already exercise the same code paths (handler methods are called from `@task` methods, so workflow-level tests cover them). Reach for the legacy framework only when you need to:

- Lock the literal JSON shape of the app server's HTTP response (e.g. as part of a platform-side contract test).
- Test handler behavior that does not flow through any `@entrypoint` workflow.

If neither applies, write the test in the in-process pattern instead.

### Quick Start

### Step 1: Copy the Example

```bash
cp -r tests/integration/_example tests/integration/my_connector
```

### Step 2: Define Your Scenarios

Edit `scenarios.py`:

```python
from application_sdk.testing.integration import (
    Scenario, lazy, equals, exists
)

def load_credentials():
    return {
        "host": os.getenv("MY_DB_HOST"),
        "username": os.getenv("MY_DB_USER"),
        "password": os.getenv("MY_DB_PASSWORD"),
    }

scenarios = [
    Scenario(
        name="auth_valid",
        api="auth",
        args=lazy(lambda: {"credentials": load_credentials()}),
        assert_that={
            "success": equals(True),
            "data.status": equals("success"),
        }
    ),
]
```

### Step 3: Create Test Class

Edit `test_integration.py`:

```python
from application_sdk.testing.integration import BaseIntegrationTest
from .scenarios import scenarios

class MyConnectorTest(BaseIntegrationTest):
    scenarios = scenarios
    server_host = "http://localhost:8000"
```

### Step 4: Run Tests

```bash
export MY_DB_HOST=localhost
export MY_DB_USER=test
export MY_DB_PASSWORD=secret
export APP_SERVER_URL=http://localhost:8000

pytest tests/integration/my_connector/ -v
```

## Core Concepts

### Scenarios

A **Scenario** defines a single test case:

```python
Scenario(
    name="auth_valid_credentials",      # Unique identifier
    api="auth",                          # API to test
    args={"credentials": {...}},         # Input arguments
    assert_that={"success": equals(True)} # Expected outcomes
)
```

### Supported APIs

| API | Endpoint | Purpose |
|-----|----------|---------|
| `auth` | `/workflows/v1/auth` | Test authentication |
| `metadata` | `/workflows/v1/metadata` | Fetch metadata |
| `preflight` | `/workflows/v1/check` | Validate configuration |
| `workflow` | `/workflows/v1/start` (default) | Start workflow |
| `config` | `/workflows/v1/config/{config_id}` | Get or update workflow config blob (object-store backed) |

**v3 Response Shapes:**

Auth responses include a `data` envelope:
```json
{"success": true, "message": "Authentication success", "data": {"status": "success", "message": "", "identities": [], "scopes": []}}
```

Preflight responses use named sub-check keys under `data` (the first character of each check name is lower-cased; spaces and inner capitals are preserved), with a `success` field inside each sub-check:
```json
{"success": true, "data": {"connectivityCheck": {"success": true, "message": "OK"}}}
```

### Lazy Evaluation

Use `lazy()` to defer computation until test execution:

```python
# BAD: Loads at import time (fails if env vars missing)
args={"credentials": load_credentials()}

# GOOD: Loads when test runs
args=lazy(lambda: {"credentials": load_credentials()})
```

Benefits:
- Tests can be defined in one environment, run in another
- Credentials loaded only when needed
- Values cached after first evaluation

> **v3 Credential Handling:** The framework automatically converts flat credential dicts to v3's key-value pair format. Developers continue to write credentials as flat dicts.

### Assertion DSL

The assertion DSL provides **higher-order functions** that return predicates:

```python
from application_sdk.testing.integration import (
    equals, exists, one_of, contains, greater_than
)

assert_that = {
    "success": equals(True),
    "data.workflow_id": exists(),
    "data.status": one_of(["RUNNING", "COMPLETED"]),
    "message": contains("successful"),
    "data.count": greater_than(0),
}
```

## Assertion Reference

### Basic Assertions

| Function | Description | Example |
|----------|-------------|---------|
| `equals(value)` | Exact equality | `equals(True)` |
| `not_equals(value)` | Not equal | `not_equals(None)` |
| `exists()` | Not None | `exists()` |
| `is_none()` | Is None | `is_none()` |
| `is_true()` | Truthy value | `is_true()` |
| `is_false()` | Falsy value | `is_false()` |

### Collection Assertions

| Function | Description | Example |
|----------|-------------|---------|
| `one_of(list)` | Value in list | `one_of(["a", "b"])` |
| `not_one_of(list)` | Value not in list | `not_one_of(["error"])` |
| `contains(item)` | Contains item | `contains("success")` |
| `not_contains(item)` | Doesn't contain | `not_contains("error")` |
| `has_length(n)` | Length equals n | `has_length(3)` |
| `is_empty()` | Empty collection | `is_empty()` |
| `is_not_empty()` | Non-empty | `is_not_empty()` |

### Numeric Assertions

| Function | Description | Example |
|----------|-------------|---------|
| `greater_than(n)` | Greater than | `greater_than(0)` |
| `greater_than_or_equal(n)` | >= | `greater_than_or_equal(1)` |
| `less_than(n)` | Less than | `less_than(100)` |
| `less_than_or_equal(n)` | <= | `less_than_or_equal(10)` |
| `between(min, max)` | In range | `between(1, 10)` |

### String Assertions

| Function | Description | Example |
|----------|-------------|---------|
| `matches(pattern)` | Regex match | `matches(r"^[a-z]+$")` |
| `starts_with(prefix)` | Starts with | `starts_with("http")` |
| `ends_with(suffix)` | Ends with | `ends_with(".json")` |

### Type Assertions

| Function | Description | Example |
|----------|-------------|---------|
| `is_type(type)` | Instance check | `is_type(str)` |
| `is_dict()` | Is dictionary | `is_dict()` |
| `is_list()` | Is list | `is_list()` |
| `is_string()` | Is string | `is_string()` |

### Combinators

Combine multiple assertions:

```python
from application_sdk.testing.integration import all_of, any_of, none_of

# All must pass
"data.name": all_of(exists(), is_string(), is_not_empty())

# At least one must pass
"data.role": any_of(equals("admin"), equals("superuser"))

# None should pass
"message": none_of(contains("error"), contains("fail"))
```

### Custom Assertions

Create your own:

```python
from application_sdk.testing.integration import custom

# Using custom()
"data.count": custom(lambda x: x % 2 == 0, "is_even")

# Or directly as a lambda
"data.value": lambda x: x > 0 and x < 100
```

## Writing Effective Scenarios

### Auth Scenarios

Test different authentication methods and edge cases:

```python
auth_scenarios = [
    # Valid credentials
    Scenario(
        name="auth_valid",
        api="auth",
        args=lazy(lambda: {"credentials": load_credentials()}),
        assert_that={
            "success": equals(True),
            "data.status": equals("success"),
        }
    ),

    # Invalid password
    Scenario(
        name="auth_invalid_password",
        api="auth",
        args=lazy(lambda: {
            "credentials": {**load_credentials(), "password": "wrong"}
        }),
        assert_that={"success": equals(False)}
    ),

    # Empty credentials
    Scenario(
        name="auth_empty",
        api="auth",
        args={"credentials": {}},
        assert_that={"success": equals(False)}
    ),
]
```

### Preflight Scenarios

Test configuration validation:

```python
preflight_scenarios = [
    # Valid configuration
    Scenario(
        name="preflight_valid",
        api="preflight",
        args=lazy(lambda: {
            "credentials": load_credentials(),
            "metadata": {"databases": ["TEST_DB"]}
        }),
        assert_that={
            "success": equals(True),
            "data.connectivityCheck.success": equals(True),
        }
    ),

    # Non-existent database
    Scenario(
        name="preflight_bad_database",
        api="preflight",
        args=lazy(lambda: {
            "credentials": load_credentials(),
            "metadata": {"databases": ["NONEXISTENT"]}
        }),
        assert_that={
            "success": equals(True),
            "data.connectivityCheck.success": equals(False),
        }
    ),
]
```

### Workflow Scenarios

Test workflow execution:

```python
workflow_scenarios = [
    # Successful workflow
    Scenario(
        name="workflow_success",
        api="workflow",
        args=lazy(lambda: {
            "credentials": load_credentials(),
            "metadata": {"databases": ["TEST_DB"]},
            "connection": {"name": "test_conn"}
        }),
        assert_that={
            "success": equals(True),
            "data.workflow_id": exists(),
            "data.run_id": exists(),
        }
    ),
]
```

## Test Class Configuration

### Basic Configuration

```python
class MyConnectorTest(BaseIntegrationTest):
    scenarios = scenarios
    server_host = "http://localhost:8000"
    server_version = "v1"
    workflow_endpoint = "/start"
    timeout = 30
```

### Dynamic Workflow Endpoint

If your workflow endpoint is different from `/start`:

```python
class MyConnectorTest(BaseIntegrationTest):
    scenarios = scenarios
    workflow_endpoint = "/extract"  # Custom endpoint
```

Or per-scenario:

```python
Scenario(
    name="workflow_custom_endpoint",
    api="workflow",
    endpoint="/custom/start",  # Override for this scenario
    args={...},
    assert_that={...}
)
```

### Setup and Teardown Hooks

```python
class MyConnectorTest(BaseIntegrationTest):
    scenarios = scenarios

    @classmethod
    def setup_test_environment(cls):
        """Called before any tests run."""
        # Create test database, schema, etc.
        cls.db = create_database_connection()
        cls.db.execute("CREATE SCHEMA test_schema")

    @classmethod
    def cleanup_test_environment(cls):
        """Called after all tests complete."""
        # Drop test database, clean up
        cls.db.execute("DROP SCHEMA test_schema CASCADE")
        cls.db.close()

    def before_scenario(self, scenario):
        """Called before each scenario."""
        print(f"Running: {scenario.name}")

    def after_scenario(self, scenario, result):
        """Called after each scenario."""
        status = "PASSED" if result.success else "FAILED"
        print(f"{scenario.name}: {status}")
```

## Running Tests

### Basic Execution

```bash
# All integration tests
pytest tests/integration/ -v

# Specific connector
pytest tests/integration/my_connector/ -v

# Single scenario
pytest tests/integration/my_connector/ -v -k "auth_valid"
```

### With Logging

```bash
# INFO level
pytest tests/integration/ -v --log-cli-level=INFO

# DEBUG level (shows API responses)
pytest tests/integration/ -v --log-cli-level=DEBUG
```

### Skip Slow Tests

Mark scenarios to skip:

```python
Scenario(
    name="workflow_large_extraction",
    api="workflow",
    args={...},
    assert_that={...},
    skip=True,
    skip_reason="Takes too long for CI"
)
```

## Best Practices

### 1. Use Lazy Evaluation for Credentials

```python
# Always use lazy() for credentials
args=lazy(lambda: {"credentials": load_credentials()})
```

### 2. Test Negative Cases

Don't just test the happy path:

```python
scenarios = [
    # Happy path
    Scenario(name="auth_valid", ...),

    # Negative cases
    Scenario(name="auth_invalid_password", ...),
    Scenario(name="auth_empty_credentials", ...),
    Scenario(name="auth_missing_username", ...),
]
```

### 3. Use Descriptive Names

```python
# Good names
"auth_invalid_password"
"preflight_missing_permissions"
"workflow_large_dataset"

# Bad names
"test_1"
"scenario_a"
```

### 4. Document Complex Scenarios

```python
Scenario(
    name="preflight_partial_permissions",
    description="Test when user has read but not write permissions",
    api="preflight",
    args={...},
    assert_that={...}
)
```

### 5. Clean Up Test Data

Use hooks to manage test data:

```python
@classmethod
def setup_test_environment(cls):
    cls.test_data = create_test_data()

@classmethod
def cleanup_test_environment(cls):
    delete_test_data(cls.test_data)
```

## Troubleshooting

### "Server not available"

Check server is running:
```bash
curl http://localhost:8000/server/health
```

### "Credentials not loading"

Verify environment variables:
```bash
env | grep MY_DB_
```

### "Assertion failed"

Run with debug logging:
```bash
pytest -v --log-cli-level=DEBUG
```

### "Timeout"

Increase timeout:
```python
class MyTest(BaseIntegrationTest):
    timeout = 60  # Increase from default 30
```

## Example Directory Structure

```
tests/integration/
├── __init__.py
├── conftest.py              # Shared fixtures
├── README.md
├── _example/                # Reference example
│   ├── __init__.py
│   ├── conftest.py
│   ├── scenarios.py
│   ├── test_integration.py
│   └── README.md
└── my_connector/            # Your connector tests
    ├── __init__.py
    ├── conftest.py
    ├── scenarios.py
    └── test_integration.py
```

## Summary

1. **Copy the example**: Start from `tests/integration/_example/`
2. **Define scenarios**: Edit `scenarios.py` with your test cases
3. **Create test class**: Inherit from `BaseIntegrationTest`
4. **Set environment variables**: Configure credentials
5. **Run tests**: `pytest tests/integration/my_connector/ -v`

The framework handles the complexity of API calls, response validation, and reporting. You focus on defining what to test and what to expect.
