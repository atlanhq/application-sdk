# Shared Integration Fixtures

SDK-side scaffolding for the integration tier described in the [Integration Testing Guide](./integration-testing.md). That guide remains the authority on *what* the tier is and why each decision in the conftest matters; this one covers the shipped code that encodes those decisions so each connector stops re-deriving them: `application_sdk.testing.integration.embedded`, the canonical conftest as a parameterized fixture set.

Nothing imports it yet. It is opt-in.

## The shared conftest

`integration_kit()` supplies everything in the canonical conftest except the three genuinely per-connector parts: the App class, the task queue, and the fixture that brings up the source.

```python
# tests/integration/conftest.py
import os

os.environ.setdefault("ATLAN_APPLICATION_NAME", "yourapp")
os.environ.setdefault("ATLAN_DEPLOYMENT_NAME", "ci")

import pytest  # noqa: E402
from application_sdk.testing.integration.embedded import integration_kit  # noqa: E402

from app.connector import YourApp  # noqa: E402 — triggers App registration


@pytest.fixture(scope="session")
def yourapp_source():
    """Whatever this connector extracts from — a container, an HTTP fake, a dict."""
    ...


_kit = integration_kit(
    app_cls=YourApp,
    task_queue="yourapp-queue",
    source_fixture="yourapp_source",
)
store_root = _kit.store_root
infrastructure = _kit.infrastructure
embedded_temporal = _kit.embedded_temporal
temporal_client = _kit.temporal_client
worker = _kit.worker
executor = _kit.executor
```

Bind all six under exactly those names — the kit's fixtures request one another by name, which is what makes the ordering rules structural. A suite whose tests already use a different name aliases instead:

```python
@pytest.fixture(scope="session")
def yourapp_executor(executor):
    return executor
```

### The source is named, not imported

`source_fixture` is resolved with `request.getfixturevalue(...)`, so the kit needs no knowledge of what the fixture yields. A testcontainer, an in-process HTTP fake, or a plain credential dict are all equally acceptable; whatever it yields is passed to `secrets=` and otherwise untouched. The contract is duck-typed on purpose — the SDK's own fake-source fixtures are one valid choice among several, not a dependency of this module.

Suites that pass credentials inline in the workflow input omit `secrets=` entirely and get an empty `MockSecretStore`.

`secrets=` serves `credential_ref` named-path and agent-spec resolution only. An input routed by legacy `credential_guid` resolves through `DaprCredentialVault` over a live daprd and never reads this store — suites for the store-guid apps must either pass credentials inline in the input, seed a GUID via the app's `/workflows/v1/dev/local-vault` dev endpoint, or stay off the kit's mocked infrastructure.

### Adopting the kit means adopting its loop scope

Every async fixture the kit builds (`embedded_temporal`, `temporal_client`, `worker`) is pinned `loop_scope="session"`. A suite's own tests must run on the session loop too, or pytest-asyncio fails or mis-schedules them. Set both `asyncio_default_fixture_loop_scope = "session"` and `asyncio_default_test_loop_scope = "session"` in the app's `pyproject.toml` `[tool.pytest.ini_options]`, or mark per-test `@pytest.mark.asyncio(loop_scope="session")`. See conformance rule T019 and `atlan-openapi-app`'s `pyproject.toml` for the reference configuration.

### What the kit decides for you

| Knob | Default | Why |
|---|---|---|
| Secret / state / storage | Mocked (`MockSecretStore`, `MockStateStore`, `create_local_store`) | The tier's job is proving the workflow executes correctly through Temporal. Real-infrastructure coverage is SDK-owned and validated centrally. |
| `data_converter` | On — `create_data_converter_for_app(app_cls)` | The converter round-trips the App's typed contracts across the workflow boundary. Its absence is a latent serialization bug, not a preference. Opt out with `data_converter=False`. |
| `enable_prometheus` | **Off** | The Temporal Rust-core runtime binds a *fixed* Prometheus port once per process, and integration jobs run `pytest -n auto --dist=loadfile` — one process per file, all racing for it. A test client needs no metrics endpoint. Opt in with `enable_prometheus=True`. |
| `APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR` | `"false"` via `preserve_artifacts=True` | Read at run time, so the kit can own it. With cleanup on, `App.on_complete()` deletes the run's local files and tracked `TRANSIENT` object-store refs, and a suite asserting on output files sees them vanish. |
| Fixture scope | Session | One source, one dev server, one worker per suite. |
| Object-store observability | `AtlanObservability._deployment_store = create_memory_store()` | Stops the periodic flush retrying and spamming warnings. |

`infrastructure_factory=` swaps the three mocked stores for any pre-built, synchronously-constructible `InfrastructureContext` — pass a callable taking the session's store root and returning it, and the kit installs it as-is. It cannot host an async lifecycle (a `daprd` sidecar, anything needing `await` or teardown), because the kit calls it synchronously. A suite that needs the production Dapr credential-vault path stays off the kit and hand-writes its own conftest — `atlan-mysql-app` is the standing exception and the reference for that shape. Mocked remains the default.

### The ordering rules, enforced rather than commented

The [conftest ordering hazards](./integration-testing.md#getting-the-conftest-right) are the failures that produce a passing-looking suite proving the wrong thing. The kit turns each into something that cannot be silently dropped:

- **Infrastructure before the worker** — the `worker` fixture depends on `infrastructure` inside the kit. There is no parameter left for an adopter to forget.
- **App registration before `create_worker`** — `create_worker` snapshots the registries at call time, so a worker built before the App import registers zero workflows, starts anyway, and then fails every workflow task for an unregistered type. Passing `app_cls` makes the registering import a precondition of calling the factory, and the factory raises `AppRegistrationMissingError` if the App is not in the registry.
- **SDK env vars before the first `application_sdk` import** — this one cannot be moved into the SDK, because importing anything under `application_sdk.testing` *is* the import that snapshots `APPLICATION_NAME` / `DEPLOYMENT_NAME` into `application_sdk.constants`. The `os.environ.setdefault` lines therefore stay at the top of the conftest — but the factory compares the snapshot against the live environment and raises `IntegrationEnvOrderingError`, naming the fix, when they disagree. An unset variable warns rather than blocks, since its only cost is mistagged observability output.

### Multi-entry-point apps

The executor shim carries `entry_point`:

```python
result = await executor.execute_app(YourApp, YourInput(...), entry_point="extract-lineage")
```

Omit it for an App with an implicit `run()` entry point. An App declaring only explicit `@entrypoint` methods registers no bare `{app}` workflow type, so it must pass one — and gets `EntryPointRequiredError` before submission rather than a run nothing claims.
