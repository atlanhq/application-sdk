# Integration-Tier Contract

What "canonical Integration tier" means for an app on this SDK. Referenced from
[`testing.md`](./testing.md) (Connector-App Testing Tiers); presence of the tier
is enforced by conformance rule `T011`, with the per-app opt-out described there.

Derived from the three reference implementations, and checked against their code
rather than restated from memory:

- `atlan-metabase-app/tests/integration/conftest.py`
- `atlan-mysql-app/tests/integration/conftest.py`
- `atlan-openapi-app/tests/integration/conftest.py`

Where a reference repo diverges from an invariant below, it is named rather than
smoothed over.

## Invariants

- **Boot Temporal via `application_sdk.dev.embedded_runtime()`.** Session-scoped.
  No suite talks to a real Temporal cluster. `embedded_runtime()` starts Temporal
  only — it starts no Dapr and sets no environment variables, so infrastructure
  setup remains the caller's job.
- **Drive the real workflow through `create_worker` / `TemporalExecutorBackend`.**
  Never a direct call to a transform function or a client method. This is the
  invariant that makes a suite an integration test rather than a unit test with
  large fixtures. Register the app *before* `create_worker` — a worker created
  first listens on a task queue nothing is registered against, and submissions
  hang rather than fail.
- **Mock state, secret, and storage infrastructure** via `MockStateStore`,
  `MockSecretStore`, and `create_local_store` / `create_memory_store`. See
  *Credential resolution* below for the one deliberate exception.
- **The source is a session-scoped fixture and a hermetic stand-in** — a pinned
  testcontainer, a generated spec, or an in-process fake — never the customer's
  live system, and never a live instance reachable by environment variable.
- **Skip, don't fail, for genuinely absent infrastructure.** Call `pytest.skip`
  at the fixture that owns the missing dependency (e.g. Docker unreachable). Do
  not yield an unconfigured fixture and leave downstream tests to fail.
- **Set SDK-affecting env vars before the first `application_sdk` import.**
  `ATLAN_APPLICATION_NAME` and `ATLAN_DEPLOYMENT_NAME` are bound into
  module-level constants at import time (`application_sdk/constants.py`), and
  several modules re-bind them into their own namespaces, so a later assignment
  has no effect on them. Use `os.environ.setdefault` at the top of `conftest.py`.
- **Set `APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR=false` when the suite
  asserts on run artifacts.** It defaults to `true`, and `App.on_complete()`
  deletes the run's local files after every run, pass or fail.

## Credential resolution

Mocked secret, state, and storage infrastructure is **normative for new
suites**, including fake-source ones. The tier's job is to prove the app's
workflow executes correctly through Temporal; re-proving that
`DaprCredentialVault` speaks the Dapr HTTP API is SDK-owned infrastructure
coverage, validated once centrally rather than per connector. A real sidecar
also costs subprocess lifecycle management, component YAML, port allocation, and
suite stability.

`atlan-mysql-app` is the documented exception: it stands up a real `daprd`
subprocess via `embedded_dapr()`, writes `secretstores.local.file` and
`bindings.localstorage` components, and routes resolution through the production
path (gated by `ATLAN_DEPLOYMENT_NAME=ci` to avoid the local-file short-circuit
in `_get_secret()`). Treat it as allowed-but-not-default: repeat it only for a
connector whose credential resolution has no other coverage.

## Reference comparison

| Aspect | metabase | mysql | openapi |
|---|---|---|---|
| **Temporal bootstrap** | `embedded_runtime(log_level="error")`, session-scoped | Same | Same, plus `create_temporal_client(..., enable_prometheus=False)` — the Rust-core runtime binds a fixed Prometheus port once per process and CI runs `-n auto --dist=loadfile` |
| **Worker** | `create_worker(client, task_queue=...)` under `async with`, session-scoped | Same | Same |
| **Source fixture** | `metabase_credentials`, session-scoped: `DockerContainer` pinned to `metabase/metabase:v0.61.2.3`, seeded through the real Metabase HTTP API (`seed_metabase`) | `mysql_database`, session-scoped `autouse`: `MySqlContainer("mysql:8.0")` seeded from `fixtures/seed.sql` | None — the source is a synthetic OpenAPI spec generated in-process (`large_spec_file`, module-scoped) |
| **Secret store** | `MockSecretStore({})`; credentials passed inline in workflow input | `MockSecretStore()`; real credentials bypass it (see below) | `MockSecretStore(secrets)`, populated from `OPENAPI_AUTH_HEADER` when set |
| **State store** | `MockStateStore()` | `MockStateStore()` | `MockStateStore()` |
| **Object store** | `create_local_store(store_root)` via `InfrastructureContext`; `AtlanObservability._deployment_store = create_memory_store()` pre-wired to silence the periodic flush | Same | Same |
| **Credential resolution** | Bypassed by design — inline in workflow input | Production `DaprCredentialVault` over an embedded `daprd` subprocess | None; auth header flows through `MockSecretStore` |
| **Executor shim** | `AppExecutor.execute_app` requires an `entry_point` argument (MetabaseApp is multi-entry-point) | No `entry_point` argument | No `entry_point` argument |
| **Env-var ordering** | `ATLAN_APPLICATION_NAME`, `ATLAN_DEPLOYMENT_NAME=ci`, `APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR=false` — all `setdefault`, all pre-import | Both `ATLAN_*` vars pre-import; cleanup interceptor set after (harmless — read at run time) | Sets neither `ATLAN_*` var |
| **Skip conditions** | `pytest.skip` at fixture level when Docker is unreachable; no external-Metabase escape hatch | Three-way: `MYSQL_HOST` → external DB; Docker → testcontainer; neither → silent yield | None |

## Known divergences in the reference repos

| Divergence | metabase | mysql | openapi |
|---|---|---|---|
| Live-source escape hatch | No | **Yes** (`MYSQL_HOST`) | n/a |
| Explicit skip on missing infra | Yes | **No** (silent yield) | n/a |
| Real Dapr sidecar | No | Yes (documented exception) | No |
| `ATLAN_*` vars set pre-import | Yes | Yes | **No** |
| Executor shim signature | `entry_point` required | absent | absent |

Three of these are worth knowing before copying a reference:

- **mysql's `MYSQL_HOST` branch** skips the testcontainer entirely and trusts
  whatever is at that host, guarded by nothing but a truthiness check on the
  environment variable — no connectivity probe, no teardown. It is a live-source
  escape hatch and new suites should not copy it. Metabase's conftest documents
  the opposite stance explicitly.
- **openapi's missing `ATLAN_*` vars** mistag its observability output as
  `default` / `local`. The blast radius is narrower than the general hazard
  suggests: `application_sdk/common/task_queue.py` deliberately reads these
  variables fresh at call time, so task-queue derivation stays correct. Set them
  anyway — the mistagging is silent.
- **The three `AppExecutor` shims have diverged in signature, not just
  implementation.** Any shared scaffold must carry metabase's `entry_point`
  parameter, or multi-entry-point apps submit to an unregistered task queue and
  hang.
