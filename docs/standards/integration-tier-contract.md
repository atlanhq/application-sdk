# Integration-Tier Contract

Referenced from [`testing.md`](./testing.md) (Connector-App Testing Tiers). This
document defines what "canonical Integration tier" means for an app on the
SDK, derived from the three reference `conftest.py` implementations that
predate any shared scaffold:

- `atlan-metabase-app/tests/integration/conftest.py`
- `atlan-mysql-app/tests/integration/conftest.py`
- `atlan-openapi-app/tests/integration/conftest.py`

It is what project milestones M3/M4 (Scaffold fake sources) review
implementations against, and what the M4 conformance check encodes. Every
claim below was checked against the actual code, not restated from memory —
where a reference repo diverges from an invariant, that is called out rather
than smoothed over.

## Side-by-side: what each conftest actually does

| Aspect | metabase | mysql | openapi |
|---|---|---|---|
| **Temporal bootstrap** | `embedded_runtime(log_level="error")`, session-scoped async fixture | Same | Same, but `create_temporal_client(..., enable_prometheus=False)` — needed because the Rust-core runtime binds a fixed Prometheus port once per process, and CI runs `-n auto --dist=loadfile` (one process per test file) |
| **Worker creation** | `create_worker(client, task_queue=...)` under `async with`, session-scoped | Same pattern | Same pattern |
| **Source fixture + scope** | `metabase_credentials`, session-scoped: `DockerContainer` pinned to `metabase/metabase:v0.61.2.3`, seeded via the real Metabase HTTP API (`seed_metabase`, 2/2/2 shape) | `mysql_database`, session-scoped + `autouse=True`: `MySqlContainer("mysql:8.0")` seeded from `fixtures/seed.sql` | **No source fixture at all.** The "source" is a synthetic OpenAPI spec generated in-process (`large_spec_file`, module-scoped) — no container, no seed step |
| **Secret store** | `MockSecretStore({})` — deliberately empty; credentials are passed **inline** in workflow input, bypassing secret-store resolution entirely | `MockSecretStore()` — also empty; real credentials never flow through it (see credential resolution) | `MockSecretStore(secrets)` — populated conditionally from `OPENAPI_AUTH_HEADER` if set |
| **State store** | `MockStateStore()` | `MockStateStore()` | `MockStateStore()` |
| **Object store** | `create_local_store(store_root)` (session tmp dir) via `InfrastructureContext`; `AtlanObservability._deployment_store = create_memory_store()` pre-wired so the periodic flush doesn't spam warnings | Same `create_local_store` + `create_memory_store` pre-wire | Same `create_local_store` + `create_memory_store` pre-wire |
| **Credential resolution** | Bypassed by design — credentials go straight into workflow input, not through `CredentialRef`/secret-store lookup | **Real `DaprCredentialVault` via an embedded `daprd` subprocess** (SDK's `embedded_dapr()`): `secretstores.local.file` (multiValued) + `bindings.localstorage` components written to temp dirs, exercising the production code path end-to-end | No credential vault involved; any auth header flows straight through `MockSecretStore` |
| **Env-var ordering** | `ATLAN_APPLICATION_NAME`, `ATLAN_DEPLOYMENT_NAME=ci`, `APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR=false` — all via `os.environ.setdefault`, all before the first `application_sdk` import | Same two `ATLAN_*` vars set before the first `application_sdk` import; `APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR` is set **after** those imports (harmless — it's read at workflow-run time, not module-load time, but it's a stylistic divergence from metabase) | **Sets neither `ATLAN_APPLICATION_NAME` nor `ATLAN_DEPLOYMENT_NAME`** — divergence, see below |
| **Skip conditions** | `pytest.skip(...)` at fixture level when Docker is unreachable; docstring states explicitly there is no external-Metabase escape hatch | Three-way fallback: (1) `MYSQL_HOST` preconfigured → external DB, (2) Docker available → testcontainer, (3) neither → yields silently, no explicit skip call | None — no container dependency to skip on; an unset `OPENAPI_AUTH_HEADER` just means private-spec-gated tests presumably skip themselves elsewhere (not in this conftest) |

## Invariants every Integration suite MUST satisfy

1. **Boot Temporal via `application_sdk.dev.embedded_runtime()`.**
   Confirmed in all three references — no suite talks to a real Temporal
   cluster.

2. **Drive the real workflow through `create_worker` / `TemporalExecutorBackend`,
   never a direct call to a transform function or client method.**
   Confirmed in all three: each defines a `TemporalExecutorBackend` wrapped in
   a small `AppExecutor` shim and executes through it. This is what makes the
   suite an integration test of the workflow, not a unit test with extra
   setup.

3. **State / secret / storage infrastructure via `MockSecretStore`,
   `MockStateStore`, `create_local_store` / `create_memory_store`.**
   Confirmed for state store and object store in all three, no exceptions.
   **Secret store is more nuanced** — see the credential-resolution decision
   below; mysql's real Dapr sidecar is a *deliberate deviation* from "just
   mock it," not a violation, but it should not be treated as the default
   pattern for new suites (see recommendation).

4. **The source is a session-scoped fixture that is a hermetic stand-in,
   never the customer's live system.**
   Metabase and mysql (testcontainer path) satisfy this. **mysql's
   `MYSQL_HOST` preconfigured-external-database branch is a live-source
   escape hatch** — it lets the suite point at a real, non-hermetic MySQL
   instance instead of the container. This is a genuine divergence from the
   invariant as stated, not a false alarm: unlike metabase's explicit
   "no external escape hatch" design, mysql's conftest has one, undocumented
   as an exception. New suites should follow metabase's stance: no live-source
   branch, container-or-skip only. openapi has no source system to diverge on.

5. **Skip, don't fail, for genuinely absent infrastructure (e.g. Docker
   unavailable); no live-source escape hatch.**
   Metabase satisfies this cleanly. mysql's third fallback branch (no
   `MYSQL_HOST`, no Docker → yield with nothing configured) does not call
   `pytest.skip` itself; it silently leaves `mysql_database` unset and
   presumably lets downstream tests fail or skip individually — weaker than
   metabase's explicit, immediate skip. New suites should skip explicitly
   at the fixture that owns the missing dependency, the way metabase does.

6. **SDK-affecting env vars set before any `application_sdk` import.**
   Metabase and mysql both set `ATLAN_APPLICATION_NAME` /
   `ATLAN_DEPLOYMENT_NAME` via `os.environ.setdefault` ahead of their first
   `application_sdk` import, because the SDK reads them at module load time
   into module-level constants. **openapi sets neither.** This is a real gap,
   not a style choice — if `application_sdk`'s module-level `APPLICATION_NAME`
   defaults to something other than `"openapi"`, anything keyed off it
   (observability, data-converter naming) is silently wrong for that suite.
   New suites must set both, matching metabase/mysql, before importing
   anything from `application_sdk`.

## The judgment call: is a real Dapr sidecar normative?

metabase and openapi both mock the secret store directly
(`MockSecretStore`) and never touch Dapr. mysql is the outlier: it stands up
a real `daprd` subprocess via the SDK's `embedded_dapr()` seam, writes
`secretstores.local.file` + `bindings.localstorage` component YAML, and
routes credential resolution through the actual production
`DaprCredentialVault` code path (gated by `ATLAN_DEPLOYMENT_NAME=ci` to avoid
the local-file short-circuit in `_get_secret()`).

**Recommendation: mocked secret/state/storage (no real Dapr sidecar) is
normative for new Integration suites, including ones built on a fake
source.** Reasoning:

- The Integration tier's job (invariant 2, above) is to prove the app's
  workflow executes correctly end-to-end through Temporal — activities,
  transforms, the extract path — not to re-prove that `DaprCredentialVault`
  correctly speaks the Dapr HTTP API. That is infrastructure the SDK owns
  and should validate once, centrally, not per-connector.
- A real sidecar adds real cost: subprocess lifecycle management, temp
  component YAML, port allocation, and a slower, flakier suite — for a
  fake source that has no interesting credential shape of its own to
  exercise.
- mysql's sidecar predates any shared fake-source pattern and looks like it
  was reaching for maximum production-fidelity in the absence of a better
  seam, not establishing a template. Treat it as the documented exception,
  not the default, until there is a specific reason (e.g. a connector whose
  credential resolution genuinely has no other coverage) to repeat it.

**This is a decision that needs explicit confirmation, not just documentation
by fiat — flagging prominently per FND-815's instructions.** If confirmed,
the M4 conformance check should treat a real Dapr sidecar in
`tests/integration/` as allowed-but-non-default, and any new scaffolded suite
(including `HttpFakeSource`-based ones from FND-816) should default to the
metabase/openapi mocked-secret-store shape.

## Divergences summary (for visibility, not folklore)

| Divergence | metabase | mysql | openapi |
|---|---|---|---|
| Live-source escape hatch | None | Yes (`MYSQL_HOST` preconfigured) | N/A (no source) |
| Explicit skip on missing infra | Yes (`pytest.skip`) | No (silent yield) | N/A |
| Real Dapr sidecar for credentials | No | Yes | No |
| `ATLAN_APPLICATION_NAME`/`ATLAN_DEPLOYMENT_NAME` set pre-import | Yes | Yes | **No** |
| Credential path exercised | Bypassed (inline in workflow input) | Production (`DaprCredentialVault`) | `MockSecretStore` directly |
