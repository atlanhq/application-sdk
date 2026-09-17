# Shared Integration Fixtures

SDK-side scaffolding for the integration tier described in the [Integration Testing Guide](./integration-testing.md). That guide remains the authority on *what* the tier is and why each decision in the conftest matters; this one covers the shipped code that encodes those decisions so each connector stops re-deriving them: `application_sdk.testing.integration.fixtures`, the canonical conftest as ordinary pytest fixtures.

Nothing imports it yet. It is opt-in.

## The shared conftest

```python
# tests/integration/conftest.py
import os

os.environ.setdefault("ATLAN_APPLICATION_NAME", "yourapp")
os.environ.setdefault("ATLAN_DEPLOYMENT_NAME", "ci")

import pytest  # noqa: E402

from application_sdk.testing.integration.fixtures import *  # noqa: E402, F403

from app.connector import YourApp  # noqa: E402


@pytest.fixture(scope="session")
def integration_app_cls() -> type[YourApp]:
    return YourApp


@pytest.fixture(scope="session")
def integration_source():
    """Whatever this connector extracts from — a container, an HTTP fake, a dict."""
    ...
```

The star-import brings in the seven kit fixtures (`store_root`, `temporary_path`, `infrastructure`, `embedded_temporal`, `temporal_client`, `worker`, `executor`) and the five `integration_*` override points. A connector overrides what it needs and gets the rest. There is no binding boilerplate and no names to get right; a suite whose tests already use a different name aliases in the ordinary way:

```python
@pytest.fixture(scope="session")
def yourapp_executor(executor):
    return executor
```

### Overrides replace, they do not wrap

Because the star-import lands these fixtures in your conftest's own namespace, pytest's usual "same name, request the original" idiom is not available — `def infrastructure(infrastructure)` is a `recursive dependency involving fixture 'infrastructure'` error, since there is no outer scope holding a base to request. An override therefore **replaces** the kit's fixture entirely.

That matters most for `infrastructure`, whose body also points observability's deployment store at an in-memory store and restores it on teardown — load-bearing, not incidental. So it is exported as a contextmanager for overrides to reuse rather than copy:

```python
from application_sdk.testing.integration.fixtures import *  # noqa: F403


@pytest.fixture(scope="session")
def infrastructure(store_root, integration_secrets):
    # Mocked secret/state stores, but a real object store.
    with kit_infrastructure(
        store_root, integration_secrets, storage=my_real_store()
    ) as ctx:
        yield ctx
```

### The override points

| Fixture | Default | Override when |
|---|---|---|
| `integration_app_cls` | **Required** — raises `AppRegistrationMissingError` telling you to override | Always. |
| `integration_task_queue` | `task_queue_from_env()` — the canonical derivation (`atlan-<app>-<deployment>`), falling back to `"<app>-queue"` when no app name is set | An explicit `ATLAN_TASK_QUEUE`-style override the env cannot express. |
| `integration_source` | `None` | The connector has a source to bring up: a testcontainer, an in-process HTTP fake, a credential dict. Whatever it yields is handed to `integration_secrets` and otherwise untouched. |
| `integration_secrets` | `{}` | Credentials are read from the secret store rather than passed inline. Receives `integration_source`; returns a `{key: json}` mapping seeded into `MockSecretStore`. |
| `integration_options` | `KitOptions()` | Any knob in the table below needs changing. |
| `temporary_path` | Not requested — the SDK default `./local/tmp/` stands | The suite asserts on a run's **local** files. Requesting it points `TEMPORARY_PATH` — the constant and every already-imported module binding of it — at a session temp dir, so run files leave the working tree and two runs of the same suite cannot read each other's artifacts. Undone on teardown. **Session-wide while it stands** — see the caveat below. |
| `infrastructure` | Mocked stores + `LocalStore` under `store_root` | The suite needs a real store. It receives `store_root`, `integration_source` and `integration_secrets`, so it can point at whatever the source fixture brought up. Must be a generator fixture so teardown still runs. |

> **`temporary_path` redirects for the whole session.** Its scope has to be `session` — the run fixtures that consume it are class-scoped, and a class-scoped fixture cannot depend on a function-scoped one. So the first test to request it moves `TEMPORARY_PATH` for every test that follows in the same process, and `pytest tests/` runs your integration and unit tiers in one process.
>
> The symptom lands somewhere else: a unit test that asserts on a default-rooted path (`local/tmp/...`) starts failing once an integration test earlier in the session opts in, and the failure names the unit test. Fix it where the assumption lives, by pinning the constant in that test rather than relying on the ambient default:
>
> ```python
> monkeypatch.setattr(constants, "TEMPORARY_PATH", "local/tmp")
> ```
>
> This has bitten twice already — two path-normalising unit tests in a connector suite, and this repo's own guard tests, which now drive `__wrapped__` function-scoped to avoid it.


`integration_secrets` serves `credential_ref` named-path and agent-spec resolution only. An input routed by legacy `credential_guid` resolves through `DaprCredentialVault` over a live daprd and never reads this store.

**That makes `credential_ref` a prerequisite for adopting these fixtures, not an optional preference.** An app still routing by `credential_guid` cannot get its credentials from this store, so it has to seed a GUID via the app's `/workflows/v1/dev/local-vault` dev endpoint against a live daprd — which reintroduces the external runtime the kit exists to remove, and leaves the suite on the legacy HTTP scenario path. Migrating the app's input contract to `credential_ref` is the work that unblocks adoption; until it lands, such a connector stays on the older framework by necessity rather than by choice. Treat a `credential_guid` input as a migration item, not as a reason the kit does not apply.

### Sources with no container: `HttpFakeSource`

SaaS and on-prem HTTP sources have no image to pull, so `integration_source` stands up a loopback HTTP server that replays reconstructed responses instead of a testcontainer. `http_fake_source_factory` owns that server's lifecycle, and ships in the same star-import as the rest of the kit:

```python
# tests/integration/conftest.py
from application_sdk.testing import FakeRequest, HttpFakeSource


@pytest.fixture(scope="session")
def integration_source(http_fake_source_factory) -> HttpFakeSource:
    fake = http_fake_source_factory(name="my-source")
    fake.route(r"/api/v1/objects", list_objects)
    fake.route(r"/api/v1/objects/(?P<object_id>[^/]+)", get_object)
    return fake


@pytest.fixture(scope="session")
def integration_secrets(integration_source: HttpFakeSource) -> dict[str, str]:
    return {"my-source": json.dumps({"host": integration_source.base_url})}
```

The fake is an `integration_source` like any other — the kit hands it to `integration_secrets` and otherwise leaves it alone — so the rest of the suite reads identically whether the source is a container or a fake.

What the connector supplies is the part that is genuinely per-source: the endpoint map, the response envelope, and any auth-signature scheme. Everything beneath — the threading server on an ephemeral loopback port, path dispatch with named parameters, the catch-all fast 404, request recording, the silenced access log — is the SDK's.

**Two counters, two scopes.** `reset_http_fake_sources` is autouse, so every test starts with a clean `requests`, `unmatched` and `hits(pattern)` — per-test questions. `unused_routes()` reads a separate lifetime counter the reset does not clear, because "is this route dead fixture weight?" can only be answered once the whole suite has run. Assert it from a session-scoped teardown, not from inside a test.

### Serving the fake to a peer, not just in-process

`start()` binds loopback on an ephemeral port by default, which is what a fixture wants and what `http_fake_source_factory` gives you. The e2e tier needs the other shape: the same fake, reachable by a *different container* on a compose network, so the connector under test dials it by service name instead of a vendor host.

```python
fake = HttpFakeSource(name="my-source", bind_host="0.0.0.0", port=8080)
```

Two things follow from a wildcard bind, both deliberate:

- **`base_url` reports loopback, not the wildcard.** `0.0.0.0` is an accept-on-every-interface instruction, not an address anything can connect to; echoing it back would hand callers a URL that fails at connect time, far from its cause.
- **A peer cannot use `base_url` at all.** There is nothing in the process from which its reachable name could be derived, so tell it out of band — the compose service name in the environment variable the connector already reads for its host.

This is what lets one corpus and one fake back both tiers: the integration suite starts the fake in-process on loopback, and e2e runs the identical route table in a container. Reverse-engineering the source's envelopes is the expensive part, and it is done once.

**Two assertions worth making in every fake-source suite.** `assert not fake.unmatched` catches an extract calling an endpoint the fake does not model — without it the test asserted against a 404. `assert not fake.unused_routes()` catches the reverse: a route carried in the fixture that nothing exercises.

### Why a star-import and not a `pytest11` plugin

A plugin loads before any conftest runs, so a module-level `application_sdk` import inside it would snapshot `APPLICATION_NAME` / `DEPLOYMENT_NAME` into `application_sdk.constants` *before* the conftest's `os.environ.setdefault` lines execute. Importing from the conftest, below those lines, is the only placement that keeps the env-before-import rule satisfiable. That constraint decides the shape.

### Adopting the fixtures means adopting their loop scope

Every async fixture (`embedded_temporal`, `temporal_client`, `worker`) is pinned `loop_scope="session"`. A suite's own tests must run on the session loop too, or pytest-asyncio fails or mis-schedules them. Set both `asyncio_default_fixture_loop_scope` and `asyncio_default_test_loop_scope` to `"session"` in `pyproject.toml`'s `[tool.pytest.ini_options]`, or mark per-test `@pytest.mark.asyncio(loop_scope="session")`. Conformance rule T019 checks this.

### What the kit decides for you

| Knob (`KitOptions`) | Default | Why |
|---|---|---|
| Secret / state / storage | Mocked (`MockSecretStore`, `MockStateStore`, `create_local_store`) | The tier's job is proving the workflow executes correctly through Temporal. Real-infrastructure coverage is SDK-owned and validated once centrally. Override `infrastructure` to change it. |
| `data_converter` | On — `create_data_converter_for_app(app_cls)` | The converter round-trips the App's typed contracts across the workflow boundary. Its absence is a latent serialization bug, not a preference. |
| `enable_prometheus` | **Off** | The Temporal Rust-core runtime binds a *fixed* Prometheus port once per process, and integration jobs run `pytest -n auto --dist=loadfile` — one process per file, all racing for it. A test client needs no metrics endpoint. |
| `preserve_artifacts` | On — **defaults** `APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR` to `"false"` when unset | Read at run time, so the kit can own it. With cleanup on, `App.on_complete()` deletes the run's local files and tracked `TRANSIENT` refs after each run, and every artifact assertion fails as "output file missing". An explicit environment value wins; a truthy one is logged as a warning naming the variable. The option is one-way — `False` leaves the variable untouched rather than forcing `"true"`. The default is scoped to the `worker` fixture's lifetime and unset again on teardown, so it cannot leak into unit tests that run later in the same process — the shape of BLDX-1283, which matters under a mixed-tier `pytest tests/`. |
| `log_level` | `"error"` | Embedded dev-server verbosity. |
| Temporal namespace | `embedded_temporal.namespace` | Threaded from the runtime to the client rather than relying on both defaulting to `"default"`. |
| Task queue | `task_queue_from_env()` | The same call `main._derive_task_queue` makes and the same value the served manifest stamps for the Automation Engine. FND-195 collapsed those to one derivation after the worker and the manifest disagreed and nothing failed loudly — the run sat unclaimed until its 24h heartbeat backstop (CONNECT-183). A local `"<app>-queue"` literal is self-consistent across this suite's own worker and executor, so it stays green while testing a queue no deployment polls. |
| Fixture scope | Session | One source, one dev server, one worker per suite. |
| Object-store observability | In-memory store for the session, restored on teardown | Stops the periodic flush retrying against a store that is not there. |

### The ordering rules, enforced rather than commented

The [conftest ordering hazards](./integration-testing.md#getting-the-conftest-right) are the failures that produce a passing-looking suite proving the wrong thing. Each is structural here:

- **Infrastructure before the worker** — `worker` depends on `infrastructure`. There is no parameter left for an adopter to forget.
- **App registration before `create_worker`** — `create_worker` snapshots the registries at call time, so a worker built before the App import registers zero workflows, starts anyway, and then fails every workflow task for an unregistered type. `worker` verifies `integration_app_cls` is in the registry before building anything and raises `AppRegistrationMissingError` otherwise.
- **SDK env vars before the first `application_sdk` import** — this one cannot be moved into the SDK, because importing anything under `application_sdk.testing` *is* the import that snapshots the constants. The `os.environ.setdefault` lines stay in the conftest, above the imports. The fixtures module compares the snapshot against the live environment when it is imported and raises `IntegrationEnvOrderingError` when they disagree.

Both checks are deliberately loud: a violation surfaces as a collection error (env ordering) or a session-fixture error (registration) that fails every test, rather than a per-test failure. A mis-ordered conftest mistags every test's observability output and an unregistered App fails every workflow task, so there is no meaningful subset to keep running.

### Multi-entry-point apps

The executor shim carries `entry_point`:

```python
result = await executor.execute_app(YourApp, YourInput(...), entry_point="extract-lineage")
```

Omit it for an App with an implicit `run()` entry point. An App declaring only explicit `@entrypoint` methods registers no bare `{app}` workflow type, so it must pass one — and gets `EntryPointRequiredError` before submission rather than a run nothing claims.
