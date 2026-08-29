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

The SDK also ships this conftest as importable fixtures — `from application_sdk.testing.integration.fixtures import *` — so a connector overrides `integration_app_cls` and `integration_source` and gets the rest, with the ordering rules below enforced rather than commented. See [Shared Integration Fixtures](./integration-fixtures.md). The canonical shape below stays the reference for what that kit does.

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
from application_sdk.execution import (
    TemporalExecutorBackend,
    create_data_converter_for_app,
    create_worker,
)
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

# A literal here is what the three reference conftests do today, and it is the
# one line of this listing worth improving on: `task_queue_from_env()` is the
# same call the worker and the served manifest make, so a literal tests a queue
# no deployment polls. The shared fixtures derive it — see
# [Shared Integration Fixtures](./integration-fixtures.md).
_TASK_QUEUE = "your-app-queue"
_CREDENTIAL_KEY = "your-app"


class AppExecutor:
    """Compatibility shim wrapping TemporalExecutorBackend for integration tests."""

    def __init__(self, backend: TemporalExecutorBackend) -> None:
        self._backend = backend

    async def execute_app(
        self,
        app_cls,
        input_data,
        *,
        execution_id_prefix: str = "",
        entry_point: str | None = None,
    ):
        from application_sdk.app.context import AppContext
        from application_sdk.execution.retry import RetryPolicy

        app_name = getattr(app_cls, "_app_name", execution_id_prefix or "app")
        context = AppContext(
            app_name=app_name, app_version="0.0.0", run_id=execution_id_prefix or app_name
        )
        return await self._backend.execute(
            app_cls,
            input_data,
            context=context,
            retry_policy=RetryPolicy(),
            entry_point=entry_point,
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

## Getting the conftest right

The conftest above encodes a few decisions that are silent when you get them wrong: nothing raises, the suite just proves something other than what you meant. All three reference connectors encode them the same way.

### Env vars before the first `application_sdk` import

`ATLAN_APPLICATION_NAME` and `ATLAN_DEPLOYMENT_NAME` are read into module-level constants the moment `application_sdk.constants` is imported:

```python
# application_sdk/constants.py
APPLICATION_NAME = os.getenv("ATLAN_APPLICATION_NAME", "default")
DEPLOYMENT_NAME = os.getenv("ATLAN_DEPLOYMENT_NAME", LOCAL_ENVIRONMENT)
```

Eleven other modules re-bind those constants into their own namespace with `from application_sdk.constants import ...`, so an assignment made after the import never reaches them. Set them at the top of `conftest.py`, above every `application_sdk` import:

```python
import os

os.environ.setdefault("ATLAN_APPLICATION_NAME", "yourapp")
os.environ.setdefault("ATLAN_DEPLOYMENT_NAME", "ci")

from application_sdk.dev import embedded_runtime  # noqa: E402
```

The blast radius is narrower than it looks, and worth knowing precisely. Task-queue derivation is **not** affected: `application_name_from_env()` and `deployment_name_from_env()` in `application_sdk/common/task_queue.py` read `os.environ` fresh at call time, deliberately. What you lose is correct tagging — a suite that skips these has its observability output attributed to `default` / `local`, silently. `atlan-openapi-app` sets neither and is mistagged today.

The one behavioural consequence is credential resolution. `DaprCredentialVault._get_secret()` (`application_sdk/infrastructure/_dapr/credential_vault.py`) branches on the snapshotted `DEPLOYMENT_NAME`: at `local` it short-circuits to reading the secrets file directly instead of going through the Dapr API. A suite that means to exercise the production path has to set `ATLAN_DEPLOYMENT_NAME` before the import, not after.

### Infrastructure before the worker

The worker fixture takes `infrastructure` as a parameter it never uses. That is deliberate, and all three reference conftests carry the same comment:

```python
@pytest_asyncio.fixture(scope="session")
async def your_app_worker(
    temporal_client,
    infrastructure: InfrastructureContext,  # noqa: ARG001 — ensures infra is wired first
):
    w = create_worker(temporal_client, task_queue=_TASK_QUEUE)
    async with w:
        yield
```

The parameter *is* the ordering rule. `infrastructure` is the fixture that calls `set_infrastructure(ctx)`, and activities resolve their stores through that context; drop the parameter and the worker is free to start before any store is wired.

### App registration before `create_worker`

`create_worker` snapshots the registries at call time:

```python
# application_sdk/execution/_temporal/worker.py
app_workflows = get_all_app_workflows()
task_activities = get_all_task_activities()
```

Those snapshots go straight into the Temporal `Worker(...)`. Importing your App class is what populates them — hence the `# noqa: F401 — triggers App registration` on the App import in the canonical conftest. A worker built before that import starts with **zero workflows** and still starts successfully, because the preflight-gate activity is registered unconditionally and the activity list is therefore never empty. It then fails every workflow task it is handed, for as long as it runs, because the workflow *type* is not registered. The task queue is whatever you passed to `create_worker` — the failure is a type mismatch, not a misrouted queue.

### pytest-asyncio loop scope

`embedded_temporal`, `temporal_client`, and `your_app_worker` are all session-scoped async fixtures. pytest-asyncio schedules a session-scoped async fixture onto a session-scoped event loop; if the *tests* consuming it are still scheduled onto the default function-scoped loop, the fixture and the test run on different loops and the suite fails or hangs rather than reporting a clean error. Set both loop-scope knobs to `"session"` in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
```

`atlan-openapi-app` is the reference (`pyproject.toml:52-53`) and conformance rule **T019** is what checks for the test-loop knob. If you would rather not change the suite-wide default, mark the affected tests individually instead:

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_workflow_extracts_tables(...):
    ...
```

Either knob alone is not enough — `asyncio_default_fixture_loop_scope` alone still leaves function-scoped *tests* on their own loop; only setting both project-wide (or the per-test marker) puts the fixtures and the tests that consume them on the same loop.

### Naming the entrypoint on a multi-entrypoint app

The canonical shim declares `entry_point`; pass it for explicit-`@entrypoint` apps:

```python
async def execute_app(
    self,
    app_cls: Any,
    input_data: Any,
    *,
    execution_id_prefix: str = "",
    entry_point: str | None = None,
) -> Any: ...
```

Pass it when the App declares explicit `@entrypoint` methods and you mean a particular one. Omitting it is safe rather than silent: `TemporalExecutorBackend` resolves the workflow name before it submits anything, and raises

- `UnknownEntryPointError` — you named an entry point the app does not declare;
- `EntryPointRequiredError` — you named none, and the app declares only explicit entry points, so it registers no bare `{app}` workflow.

Both guards are pre-submission by design. The resolver's docstring says why: *"Temporal accepts a start request for an unregistered type either way — the run opens, nothing claims it, and the caller awaits a listener that never comes."* An App with an implicit `run()` entry point needs no `entry_point=`; the bare `{app}` type is registered and resolution falls through to it.

See [Entry points — Testing each entry point](../concepts/entry-points.md#testing-each-entry-point).

### Keep the artifacts you assert on

`APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR` defaults to `true`. With it on, `App.on_complete()` runs two cleanups after every run, pass or fail, and gates **both** on that single flag:

- `cleanup_files` — the run's local files, including what a `create_local_store` root holds;
- `cleanup_storage` — object-store objects: every tracked `TRANSIENT`-tier ref plus its `.sha256` sidecar. (The run-scoped prefix sweep is a separate opt-in, `StorageCleanupInput(include_prefix_cleanup=True)`; `on_complete()` does not request it.)

So a suite that opens output files and asserts on their contents — the pattern in Step 2 — has to turn it off, alongside the other env vars:

```python
os.environ.setdefault("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "false")
```

This one is read at run time rather than at import, so its position is not load-bearing: `atlan-metabase-app` sets it pre-import, `atlan-mysql-app` after the imports, and `atlan-openapi-app` not at all.

## Markers and CI tiering — the directory *is* the boundary

The reusable Tests workflow runs the two tiers as **separate, directory-scoped jobs**: the unit job runs `pytest tests/unit`, and the integration job runs `pytest tests/integration/`. Neither re-selects by marker. Two rules follow from that:

- **Mark integration tests with the single standard `integration` marker** (conformance **T001**). It documents intent and lets a developer skip the heavy tier locally with `-m "not integration"`. Use one marker, not a family of bespoke ones (`s3_integration`, `azure_integration`, …).
- **Do _not_ `addopts`-deselect that marker** (conformance **T018**). Because the integration job selects by path, an `addopts = "-m 'not integration'"` in `pyproject.toml` is applied to `pytest tests/integration/` too and removes those tests from the only job meant to run them. If it deselects *every* test in the directory, the job collects nothing and fails with **pytest exit code 5** (`no tests ran`); if it deselects some, they silently run in no tier at all.

```toml
# pyproject.toml — the standard shape (see atlan-mysql-app, atlan-metabase-app)
[tool.pytest.ini_options]
markers = ["integration: requires external services; deselect locally with -m 'not integration'"]
# NO addopts '-m not ...' deselection — the directory is the tier boundary.
```

**Tests that need an external service** (an emulator, a live source) should **self-skip at runtime** when it is unavailable — a module-scoped autouse fixture that probes the endpoint and calls `pytest.skip(...)`:

```python
@pytest.fixture(scope="module", autouse=True)
def require_minio() -> None:
    """Skip this module's tests when MinIO isn't reachable (bare local run)."""
    if not _reachable(os.environ.get("AWS_ENDPOINT_URL", "http://localhost:9000")):
        pytest.skip("MinIO not reachable; CI provisions it via services-script")
```

This keeps a bare local `pytest tests/integration/` green without the service while CI (which provisions it) runs the tests — without hiding anything from the CI tier, which an `addopts` deselect would. See [`atlan-openapi-app`](https://github.com/atlanhq/atlan-openapi-app/blob/main/tests/integration/test_s3_download.py) for the `require_minio` / `require_azurite` pattern.

## What to test

| Scenario | What to assert |
|---|---|
| Happy path | Output dataclass fields are populated; transformed JSONL files exist; records carry the typed attributes that downstream nodes (QI, publish) read. |
| Filters (`include_*` / `exclude_*`) | Workflow accepts the typed filter shape and completes. Filter logic itself is unit-tested elsewhere; integration test just verifies the contract threads through. |
| Inline credentials path | `credentials=[{"key":...}]` works as a fallback when `CredentialRef` is absent. Catches regressions in `build_credential_ref` and the per-task credential routing. |
| Second `@entrypoint` (if your App has one) | Smoke-test that it accepts its typed input and returns a well-formed output even on empty intermediate state (e.g. empty QI prefix). |
| Error paths | Misconfigured credentials surface as a typed `AppError` subclass, not a bare `Exception`. |

Refer to the connector adopters' suites for working examples — [`atlan-openapi-app/tests/integration/test_openapi.py`](https://github.com/atlanhq/atlan-openapi-app/blob/main/tests/integration/test_openapi.py), [`atlan-metabase-app/tests/integration/test_metabase_workflow.py`](https://github.com/atlanhq/atlan-metabase-app/blob/main/tests/integration/test_metabase_workflow.py).

## What "the real source" means here

Conformance rule **T011** requires every app to ship this tier and describes it as *connecting to the real source* — at WARN tier today (see [`docs/standards/testing.md`](../standards/testing.md)). Read "real source" as a real **engine**: a containerised, version-pinned, seeded instance of the actual software, which is what the two container-backed references do. It does not mean a customer's instance, and this tier is not where you reach one.

Pointing a suite at a source you control is fine — the canonical conftest seeds its credentials from `E2E_YOURAPP_HOST` for exactly that. What to avoid is a branch that *silently prefers* whatever happens to be at that host over the hermetic engine; see [`MYSQL_HOST`](#how-the-three-reference-connectors-differ) below.

For the layers around the source — secret, state and object stores — **mock them**: `MockSecretStore`, `MockStateStore`, `create_local_store`, plus `create_memory_store` for the observability deployment store. This is the default for new suites, fake-source ones included. The tier's job is to prove the app's workflow executes correctly through Temporal; proving that `DaprCredentialVault` speaks the Dapr HTTP API is SDK-owned coverage, validated once centrally rather than once per connector — and a real sidecar costs subprocess lifecycle, component YAML, port allocation and suite stability.

`atlan-mysql-app` is the standing exception: it boots a real `daprd` subprocess via `embedded_dapr()`, writes `secretstores.local.file` and `bindings.localstorage` components, and routes resolution through the production `DaprCredentialVault` path (which is why it sets `ATLAN_DEPLOYMENT_NAME=ci` pre-import). The argument for keeping it is narrow but real: that suite is currently the fleet's only coverage of the `DEPLOYMENT_NAME` branch in `_get_secret()` that picks between the local-file short-circuit and the full Dapr API — an observable behaviour switch, not pure SDK plumbing, so something has to exercise it. Treat it as retained until the SDK covers that branch centrally, not as a pattern to copy.

## How the three reference connectors differ

The three suites are close enough to copy from, and these are the places where you should decide rather than inherit. Each cell is checked against that repo's `tests/integration/conftest.py`.

| Aspect | `atlan-metabase-app` | `atlan-mysql-app` | `atlan-openapi-app` |
|---|---|---|---|
| **Temporal** | `embedded_runtime(log_level="error")`, session-scoped | Same | Same |
| **Client** | `create_temporal_client(host=…, data_converter=create_data_converter_for_app(MetabaseApp))` | Same, with `MySQLApp` | `create_temporal_client(host=…, enable_prometheus=False)` — **no data converter**; the flag stops three `-n auto --dist=loadfile` processes racing for one fixed Prometheus port |
| **Worker** | `create_worker(client, task_queue=_TASK_QUEUE)` under `async with`, session-scoped, depends on `infrastructure` | Same | Same |
| **Source** | `metabase_credentials`, session-scoped: `DockerContainer("metabase/metabase:v0.61.2.3")`, seeded over the real Metabase HTTP API | `mysql_database`, session-scoped `autouse`: `MySqlContainer(image="mysql:8.0", username=…, password=…, root_password=…, dbname="ecommerce")`, seeded from `fixtures/seed.sql` | None — the app's input *is* a spec, generated in-process per test |
| **Secret store** | `MockSecretStore({})`; credentials passed inline in the workflow input | `MockSecretStore()`, unused — resolution goes through Dapr | `MockSecretStore(secrets)`, seeded from `OPENAPI_AUTH_HEADER` when set |
| **State store** | `MockStateStore()` | Same | Same |
| **Object store** | `create_local_store(store_root)` in the `InfrastructureContext`, plus `AtlanObservability._deployment_store = create_memory_store()` | Same | Same |
| **Credential resolution** | Bypassed by design (inline) | Production `DaprCredentialVault` over an embedded `daprd` | `MockSecretStore` only |
| **Executor shim** | `entry_point` present, passed a value | Parameter present (canonical shim), unused | Parameter present (canonical shim), unused |
| **Env vars** | `ATLAN_APPLICATION_NAME`, `ATLAN_DEPLOYMENT_NAME=ci`, `APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR=false` — all `setdefault`, all pre-import | Both `ATLAN_*` pre-import; cleanup interceptor `setdefault` after the imports | Sets none of the three |
| **Skips** | One `pytest.skip(…, allow_module_level=True)` when Docker is unreachable | None | None |
| **Fixture scope** | All session | All session | All session except `large_spec_file` (module) — a ≥100 MiB stress payload for the large-spec tests, not the suite's source |

Three of those are worth a decision:

- **`MYSQL_HOST` is a live-source escape hatch — don't copy it.** `mysql_database` consults `_mysql_host_preconfigured()` first, which is nothing but `bool(os.environ.get("MYSQL_HOST"))`, and returns early:

  ```python
  if _mysql_host_preconfigured():
      logger.info("Using preconfigured MySQL at %s", os.environ["MYSQL_HOST"])
      yield
      return
  ```

  The variable therefore wins over the testcontainer whenever it happens to be set, with no connectivity probe and nothing to tear down. `atlan-metabase-app` takes the opposite position explicitly in its module docstring — *"Integration tests ALWAYS use a local testcontainer — there's no external-Metabase escape hatch"* — and that is the one to follow.

- **Self-skip when the engine is missing.** Recommended, and only `atlan-metabase-app` does it today: call `pytest.skip(…, allow_module_level=True)` from the fixture that owns the missing dependency. `mysql_database` instead yields anyway when neither `MYSQL_HOST` nor Docker is available, so every downstream test fails on a connection error rather than reporting a skip. Same rule as the `require_minio` pattern above.

- **Pass a data converter unless you know you don't need one.** metabase and mysql both build `create_data_converter_for_app(App)` and hand it to `create_temporal_client`; openapi passes none. The converter is what carries the App's typed inputs and outputs across the workflow boundary, so if your contracts hold types Temporal's default JSON conversion does not round-trip, this divergence is the bug you would otherwise chase.

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

### Naming the entrypoint on a multi-entrypoint app

`api="workflow"` means "POST `/workflows/v1/start`". It does not say **which**
`@entrypoint` gets started. Omitting the selector does not fail: the app resolves
its default entrypoint (the one marked `default=True`, else the alphabetically
first), so a suite meaning to exercise the miner can run the crawler instead and
pass, with nothing surfacing the mismatch.

Declare it with `entrypoint=`:

```python
Scenario(
    name="miner_workflow",
    api="workflow",
    entrypoint="miner",          # → POST /start?entrypoint=miner
    args={"miner_start_time_epoch": "0"},
    assert_that={"success": equals(True)},
)
```

Or suite-wide, when every scenario in the class targets one entrypoint:

```python
class MinerIntegrationTest(BaseIntegrationTest):
    entrypoint = "miner"         # applies to every workflow scenario
    scenarios = miner_scenarios
```

Resolution order, most specific first:

| | |
|---|---|
| `scenario.endpoint` | full path override, wins outright (may carry its own query string) |
| `scenario.entrypoint` | appended to `workflow_endpoint` |
| `cls.entrypoint` | suite-wide default |
| neither | bare `workflow_endpoint`, exactly as before |

The value is URL-encoded and joined with `&` when `workflow_endpoint` already has
a query string. A single-entrypoint app can leave all of this unset — the target
is unambiguous and the emitted endpoint is unchanged.

> `endpoint="/start?entrypoint=miner"` still works and still wins, but prefer
> `entrypoint=` — it is what makes "which product workflow does this scenario
> cover" machine-readable instead of recoverable only by reading the source.

**In-process tests: pass `entry_point=` too.** The recommended in-process pattern
resolves the workflow type from the app name plus the entry point, and a
multi-entrypoint app registers only `{app}:{entry-point}` — never the bare
`{app}`. Submitting without `entry_point` therefore used to open a run no worker
claims and await it forever; the executor now raises `EntryPointRequiredError`
instead, but the fix is still to name it:

```python
await executor.execute(MyApp, input_data, entry_point="extract-metadata")
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
