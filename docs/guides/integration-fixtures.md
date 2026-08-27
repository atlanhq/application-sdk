# Shared Integration Fixtures and the Golden-Corpus Contract

Two pieces of SDK-side scaffolding for the integration tier described in the [Integration Testing Guide](./integration-testing.md). That guide remains the authority on *what* the tier is and why each decision in the conftest matters; this one covers the shipped code that encodes those decisions so each connector stops re-deriving them.

- `application_sdk.testing.integration.embedded` — the canonical conftest as a parameterized fixture set.
- `application_sdk.testing.integration.corpus` — one declared layout, one env var, and a loader for in-repo golden corpora.

Nothing imports either module yet. Both are opt-in.

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

### What the kit decides for you

| Knob | Default | Why |
|---|---|---|
| Secret / state / storage | Mocked (`MockSecretStore`, `MockStateStore`, `create_local_store`) | The tier's job is proving the workflow executes correctly through Temporal. Real-infrastructure coverage is SDK-owned and validated centrally. |
| `data_converter` | On — `create_data_converter_for_app(app_cls)` | The converter round-trips the App's typed contracts across the workflow boundary. Its absence is a latent serialization bug, not a preference. Opt out with `data_converter=False`. |
| `enable_prometheus` | **Off** | The Temporal Rust-core runtime binds a *fixed* Prometheus port once per process, and integration jobs run `pytest -n auto --dist=loadfile` — one process per file, all racing for it. A test client needs no metrics endpoint. Opt in with `enable_prometheus=True`. |
| `APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR` | `"false"` via `preserve_artifacts=True` | Read at run time, so the kit can own it. With cleanup on, `App.on_complete()` deletes the run's local files and tracked `TRANSIENT` object-store refs, and a suite asserting on output files sees them vanish. |
| Fixture scope | Session | One source, one dev server, one worker per suite. |
| Object-store observability | `AtlanObservability._deployment_store = create_memory_store()` | Stops the periodic flush retrying and spamming warnings. |

Real infrastructure stays available and stays explicit: pass `infrastructure_factory=`, a callable taking the session's store root and returning an `InfrastructureContext` the kit installs as-is. Mocked remains the default.

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

## The golden-corpus contract

A golden corpus is the sanitized fixture tree an integration suite reads when it has no live source: recorded source payloads to feed the transform, and the expected records the transform should produce.

```
$E2E_GOLDEN_ROOT/            # or an in-repo default the suite passes
  [<tenant>/]                # optional — declare tenant_level=True to use it
    raw/                     # the transform's INPUT
    transformed/             # what the transform should produce
```

```python
from pathlib import Path

import pytest
from application_sdk.testing.integration.corpus import GoldenLayout, require_golden_corpus

_LAYOUT = GoldenLayout(
    stages=("raw", "processed", "transformed"),
    input_stage="processed",
    tenant_level=True,
)


@pytest.fixture(scope="session")
def corpus():
    return require_golden_corpus(
        layout=_LAYOUT,
        default_root=Path(__file__).parent / "fixtures" / "golden",
    ).for_tenant("tenant-a")


def test_transform_input_present(corpus) -> None:
    assert corpus.records(corpus.layout.input_stage)
```

Four rules, each collapsing a divergence that appeared across connector suites written independently:

- **One env var: `E2E_GOLDEN_ROOT`.** Every test-harness variable in `application_sdk/testing/` uses the `E2E_` prefix (`E2E_SOURCE_AVAILABLE`, `E2E_TENANT_DEPLOYMENT_NAME`, `E2E_WORKER_HEALTH_URL`, the `E2E_<DATASOURCE>_*` credential family). `ATLAN_*` is runtime SDK configuration read into module constants — a different contract. Not one variable per connector.
- **`raw/` means "the transform's input"**, not "untouched bytes from the source". A connector with a genuine post-processing stage declares it — `stages=(..., "processed", ...)`, `input_stage="processed"` — so which stage feeds the transform is a stated fact rather than something a reader infers from a test file. No fifth word needed.
- **The tenant level is optional**, and off by default. Connectors with no tenant axis must not invent a synthetic directory to satisfy a loader, and a corpus with one may name its tenant directories anything.
- **Missing and malformed are different failures.** No corpus configured — `E2E_GOLDEN_ROOT` unset and no default root on disk — is the declared-absent case, and `require_golden_corpus` skips, the single skip idiom for this tier, matching the [skip-not-fail contract](./integration-testing.md#markers-and-ci-tiering-the-directory-is-the-boundary). A corpus that exists but does not match its declared layout raises with the offending path named: a missing stage directory, a stage holding no files, an unparseable file. An empty stage is an error, never an empty list — a loader that silently yields nothing turns a broken fixture tree into a passing test.

### Scope boundary

This contract governs the **in-repo fixture tree** only. The upstream source buckets these corpora were captured from genuinely differ — legacy Argo writes `{extracted,transformed}-metadata`, an SDK app writes `{raw,transformed}` — and neither is ours to rename. Capture from whatever the bucket holds; commit it under the layout above.

### Formats

JSON (one record object or an array of them), NDJSON (`.ndjson` / `.jsonl`), CSV (header row required), and parquet. `read_records(path)` dispatches on the suffix and every failure names the file, and for NDJSON the line.

Parquet needs `pyarrow`, which ships in the `[sql]` and `[incremental]` extras rather than the SDK core, so it is imported lazily and its absence raises `GoldenParquetSupportError` naming the extra to install.

Comparing a run's output against the corpus's expected records is a separate concern with its own helpers; this module declares the tree and reads it.
