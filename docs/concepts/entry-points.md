# Entry Points

This page describes how v3 applications are started. The v2 pattern of instantiating `BaseApplication`, calling `setup_workflow()`, and `start()` is replaced by a CLI and a simple programmatic helper.

## CLI: application-sdk

The primary entry point in production is the `application-sdk` CLI:

```bash
# Production -- separate pods for handler and worker
application-sdk --mode handler --app my_package.apps:MyExtractor
application-sdk --mode worker  --app my_package.apps:MyExtractor

# Local dev / SDR -- combined in one process
application-sdk --mode combined --app my_package.apps:MyExtractor
```

### Three Modes

| Mode | What runs | Typical use |
|------|-----------|-------------|
| `worker` | Temporal worker only | Production worker pods |
| `handler` | HTTP handler service only | Production handler pods |
| `combined` | Both in one process | Local dev, SDR |

### App Resolution

The `--app` flag takes a Python module path in `module:ClassName` format. The CLI imports the module and looks up the `App` subclass.

Alternatively, set the `ATLAN_APP_MODULE` environment variable. This is mandatory in production -- the entrypoint hard-fails at startup if it is not set and `--app` is not provided.

## Dockerfile Configuration

The base image (`registry.atlan.com/public/app-runtime-base:3`) includes the `application-sdk` CLI, Dapr, and the entrypoint. You do not need a custom `ENTRYPOINT`, `CMD`, or `entrypoint.sh`. The base image handles mode selection at runtime.

```dockerfile
# Application-sdk v3 base image (Chainguard-based)
FROM registry.atlan.com/public/app-runtime-base:3

WORKDIR /app

# Install dependencies first (better caching)
COPY --chown=appuser:appuser pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000,gid=1000 \
    uv venv .venv && \
    uv sync --locked --no-install-project

# Copy application code
COPY --chown=appuser:appuser . .

# App-specific environment variables
ENV ATLAN_HANDLER_PORT=8000
ENV ATLAN_APP_MODULE=app.connector:MyApp
ENV ATLAN_CONTRACT_GENERATED_DIR=app/generated

```

`ATLAN_CONTRACT_GENERATED_DIR` tells the SDK where to find the generated contract JSON files (configmaps, manifest). Place these files inside your repo's `app/generated/` directory.

The `--app` CLI flag takes precedence over the env var, but hardcoding `ATLAN_APP_MODULE` in the Dockerfile is the recommended approach so the value is locked to the image.

## Programmatic: run_dev_combined()

For local development and integration tests, use `run_dev_combined()`:

```python
import asyncio
from application_sdk.main import run_dev_combined
from my_package.apps import MyExtractor

asyncio.run(run_dev_combined(MyExtractor))
```

This starts both the Temporal worker and the HTTP handler service in a single process. It derives the module path automatically from the class.

To inspect local workflows in Temporal Web UI, enable it explicitly. The UI
uses port `8233` by default:

```python
asyncio.run(run_dev_combined(MyExtractor, temporal_ui=True))
```

### Custom Secrets for Local Dev

Pass credentials directly for local development — `run_dev_combined` auto-provisions them
through the local vault so the flow mirrors production exactly:

```python
from application_sdk.main import run_dev_combined

asyncio.run(run_dev_combined(
    MyExtractor,
    credentials={"host": "localhost", "port": "5432", "authType": "basic",
                 "username": "dev", "password": "dev-secret"},
))
```

## Worker Auto-Discovery

You no longer register workflow or activity classes explicitly. The worker discovers everything at startup:

1. When Python imports your `App` subclass, `App.__init_subclass__` registers it in `AppRegistry` and its `@task` methods in `TaskRegistry`.
2. `create_worker()` reads both registries and configures the Temporal worker automatically.

If you need a worker handle directly (for integration tests):

```python
from application_sdk.execution import create_temporal_client, create_worker

client = await create_temporal_client(host="localhost:7233")  # pass host/namespace explicitly
worker = create_worker(client)                                 # discovers all App subclasses automatically
await worker.run()
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ATLAN_APP_MODULE` | Yes (production) | Python module path, e.g. `app.app:MyExtractor` |
| `ATLAN_CONTRACT_GENERATED_DIR` | Recommended | Path to generated contract JSON files |
| `ATLAN_TEMPORAL_HOST` | Recommended | Temporal server host (defaults to `localhost:7233`; v2 fallback: `ATLAN_WORKFLOW_HOST` + `ATLAN_WORKFLOW_PORT`) |
| `ATLAN_TEMPORAL_NAMESPACE` | Recommended | Temporal namespace (defaults to `default`; v2 fallback: `ATLAN_WORKFLOW_NAMESPACE`) |

---

## Multiple Entry Points Per App

A single `App` can expose multiple independently-triggerable workflows by decorating methods with `@entrypoint` instead of overriding `run()`.

```python
from application_sdk.app import App, entrypoint, task
from application_sdk.contracts import Input, Output

class ExtractionInput(Input):
    connection_qualified_name: str = ""

class ExtractionOutput(Output):
    count: int = 0

class MiningInput(Input):
    connection_qualified_name: str = ""

class MiningOutput(Output):
    count: int = 0

class SnowflakeApp(App):
    @task(timeout_seconds=3600)
    async def fetch_tables(self, input: ExtractionInput) -> ExtractionOutput:
        ...

    @entrypoint
    async def extract_metadata(self, input: ExtractionInput) -> ExtractionOutput:
        return await self.fetch_tables(input)

    @entrypoint
    async def mine_queries(self, input: MiningInput) -> MiningOutput:
        ...
```

**Contract requirements** enforced by the decorator:
- Exactly one parameter extending `Input` (no `*args`/`**kwargs`)
- Return type extending `Output`
- Input/Output Pydantic models must be defined at module level — not inside functions or under `from __future__ import annotations`

### Workflow naming

| App shape | Temporal workflow name |
|-----------|----------------------|
| Single `run()` override | `{app-name}` (backward-compat, no colon) |
| `@entrypoint extract_metadata` | `{app-name}:extract-metadata` |
| `@entrypoint mine_queries` | `{app-name}:mine-queries` |

Method names are converted to kebab-case automatically. Override with `@entrypoint(name="custom-name")`.

#### Accepting an established legacy workflow type

A single-`run()` app gets a bare workflow type for free. A multi-entry-point app cannot — every entry point is prefixed. That is a problem only when external callers already dispatch a bare type and cannot be changed in step with the app: `StartWorkflowExecution` does not validate registration, so a caller on an unregistered name gets a successful start, no worker claims the task, and the run sits open until the execution timeout.

`App.legacy_workflow_types` declares such types as **inbound-only aliases**:

```python
class QueryIntelligenceApp(App):
    name = "query-intelligence"
    legacy_workflow_types = {
        "QueryIntelligenceWorkflow": "query-intelligence",
        "KeifuWorkflow": "keifu",
    }

    @entrypoint(default=True)
    async def query_intelligence(self, input: QIInput) -> QIOutput: ...

    @entrypoint(name="keifu")
    async def keifu(self, input: KeifuInput) -> KeifuOutput: ...
```

| Registered Temporal type | Reaches |
|---|---|
| `query-intelligence:query-intelligence` | `query_intelligence` (canonical — the SDK dispatches this) |
| `QueryIntelligenceWorkflow` | `query_intelligence` (legacy alias, accepted only) |
| `query-intelligence:keifu` | `keifu` (canonical) |
| `KeifuWorkflow` | `keifu` (legacy alias, accepted only) |

An alias is **accepted, never produced**. Every SDK-initiated dispatch — the executor, `POST /workflows/v1/start`, the event route — emits the canonical type. Because `workflow.info().workflow_type` reflects the name the *caller* used, the `temporal.workflow.type` telemetry dimension counts exactly the callers still on the legacy name, and each such run logs a warning naming the alias and its canonical type. Watch the count drain to zero, then delete the alias — and not before: removal also needs the alias's open workflows drained or pinned to the previous worker build with Temporal worker versioning, since an open workflow replays against whichever worker still registers its type.

What does **not** move: `?entrypoint=` still selects by entry-point name, task activity names keep their `{app-name}:{task-name}` prefix, and `App.name` is untouched — so state and storage namespaces stay put. The canonical type cannot be changed; an alias is a migration surface, not a naming lever.

Registration fails loudly on a declaration mistake. At class definition: an alias that restates a canonical type, targets an unknown entry point, equals an entry-point name, or collapses to another type's generated class name (`-` and `:` both become `_`) — and, independent of aliases, an entry-point name that equals a sibling's canonical type (only reachable as an explicit entry point named exactly like the app while an implicit `run()` claims the bare app name). At worker startup: an alias that claims an SDK-reserved `sdr:*` handler type or duplicates a type another app on the same worker registers.

> **The workflow-type namespace is global across workers.** An alias may be colon-qualified (for example `teradata-app:crawler`) — that is the shape a migrating app preserves, and it does not make the type canonical. Two apps on *different* workers that register the same type are not caught at startup (collision checks are per-worker); raw Temporal dispatch by type name then lands on whichever worker registered it. Treat aliases as globally unique: coordinate them across apps the same way you coordinate any shared Temporal type.

> **Declaration site.** The class attribute is what registers the alias — the class **body** specifically: the declaration is read once at class definition, and worker startup refuses to boot if a post-definition assignment or mutation diverged from what registration recorded. A contract-carrying app declares the same aliases a second time in the app contract manifest, via the toolkit's `legacyWorkflowTypes` block, which is the contracted declaration site; conformance `K015` fails the app when the two disagree, and `P016` routes off the manifest copy. An app with no contract tree declares them in the class attribute alone.
>
> ```pkl
> legacyWorkflowTypes {
>   new LegacyWorkflowTypeSpec {
>     alias = "KeifuWorkflow"
>     entrypoint = "keifu"
>   }
> }
> legacyWorkflowTypesRemovalVersion = "4.2.0"
> ```
>
> The block is app-level, matching the class attribute: for a multi-entrypoint bundle the **same** block goes on every entry point's contract, so each generated manifest carries an identical copy. The bundle root renders no manifest and refuses the declaration at eval time.

> **Expiry.** `legacy_workflow_types_removal_version = "4.2.0"` is an opt-in deadline: once the installed SDK reaches it, registration fails while aliases remain declared — keeping them becomes a loud decision rather than drift. Leave it unset for aliases with a wide external caller set; removal then gates on the `temporal.workflow.type` legacy-caller count reaching zero. The expiry is app-level, not per-alias, and a contract-carrying app declares the same value in `legacyWorkflowTypesRemovalVersion`.

### HTTP dispatch

Trigger a specific entry point via the `?entrypoint=` query parameter on `POST /workflows/v1/start`:

```bash
# Trigger extract-metadata
curl -X POST 'http://localhost:8000/workflows/v1/start?entrypoint=extract-metadata' \
  -H "Content-Type: application/json" \
  -d '{"credentials": {...}, "connection": {...}, "metadata": {...}}'

# Trigger mine-queries
curl -X POST 'http://localhost:8000/workflows/v1/start?entrypoint=mine-queries' \
  -H "Content-Type: application/json" \
  -d '{"credentials": {...}, "connection": {...}, "metadata": {...}}'
```

When `?entrypoint=` is omitted the SDK resolves the default entry point automatically — see [Default entrypoint resolution](#default-entrypoint-resolution) below. Pass `?entrypoint=<name>` to target a specific entry point explicitly.

> **Transitional fallback:** The body field `workflow_type` is accepted for backward compatibility with legacy Heracles callers. Query param takes precedence if both are provided. The body field will be removed in a future release.

The two selectors resolve different namespaces, on purpose:

- **`?entrypoint=` (canonical)** resolves entry-point **names only**, on every route. An alias or a raw workflow type through it is a 400 — the alias namespace never becomes a second permanent name on the SDK's own HTTP surface, and `/start` and `/input-contract` accept exactly the same selector strings.
- **body `workflow_type` (deprecated)** resolves a name first, then the app's registered Temporal workflow types (canonical and legacy aliases) — that field exists for a legacy caller who genuinely holds a workflow type. The order can never matter: registration keeps names and types disjoint (an alias may not equal an entry-point name, and an entry-point name may not equal a sibling's canonical type).

### Default entrypoint resolution

The SDK resolves which entry point to invoke when `?entrypoint=` is omitted, following these rules in order:

| App shape | Default resolution |
|---|---|
| `run()` only | `run()` is the implicit default (backward compat) |
| Single `@entrypoint` | that entry point is the default (len==1 rule) |
| Multiple `@entrypoint`s, none explicit | first alphabetically is auto-marked default |
| Multiple `@entrypoint`s, one `default=True` | that one is the default |
| Multiple `@entrypoint`s, multiple `default=True` | error at class definition time |
| `run()` + `@entrypoint`(s) | `run()` is always the default; `@entrypoint(default=True)` raises |

The `default=True` flag on `@entrypoint` is only meaningful when the app has multiple `@entrypoint` methods and no `run()` override. Mark it explicitly if you want a specific non-alphabetical entry point to be the default:

```python
class SnowflakeApp(App):
    @entrypoint                       # not the default — 'e' < 'm' alphabetically
    async def mine_queries(self, input: MiningInput) -> MiningOutput: ...

    @entrypoint(default=True)         # explicitly the default
    async def extract_metadata(self, input: ExtractionInput) -> ExtractionOutput: ...
```

`run()` and `@entrypoint` methods can coexist in the same class. In that case `run()` permanently holds the default regardless of any `default=True` flag on `@entrypoint`.

### Shared infrastructure

All entry points on the same App share:
- `@task` methods (registered as Temporal activities once)
- The HTTP handler (`/auth`, `/check`, `/metadata`)
- `AppContext` (secrets, state, storage)
- `on_complete()` lifecycle hook — fires after every entry point, on success or failure

### Manifest per entry point

For multi-entry-point apps, each entry point has its own `manifest.json` in a subfolder named after the entry point (kebab-case, matching `ep.name`):

```
ATLAN_CONTRACT_GENERATED_DIR/
  extract-metadata/
    manifest.json
  mine-queries/
    manifest.json
```

Retrieve a specific manifest with:

```bash
GET /workflows/v1/manifest?entrypoint=<entry-point-name>
```

Behaviour (with `?entrypoint=<entry-point-name>`):
- Returns 400 if `<entry-point-name>` fails validation (`^[a-zA-Z][a-zA-Z0-9_-]*$`).
- Returns 404 if that entry point's subfolder or `manifest.json` is missing.
- The `?entrypoint=` token is the same kebab-case identifier used on `POST /workflows/v1/start` — one naming convention, two endpoints.

Without a query param, `GET /workflows/v1/manifest` serves the first candidate with a manifest on disk: the default entry point (if any), then the explicit (non-implicit) entry points in alphabetical order. So a `run()` + `@entrypoint(s)` app — whose implicit `run` default has no manifest dir — resolves to its first explicit entry point (e.g. `crawler`) with a **200** rather than 404. It 404s only when no candidate has a servable `manifest.json`. For single-entry-point apps this is just that app's manifest, as before.

> **Configmap discovery** also benefits from the subfolder layout: the handler uses `rglob("*.json")` so configmap files can live inside per-entry subfolders alongside the manifests.

### One authority for the generated tree's shape

Three facts about `app/generated/` are needed in more than one place, so they
live in `application_sdk/app/generated_tree.py` and nowhere else:

| Function | Answers |
| --- | --- |
| `generated_layout(dir)` | `multi` (per-entry-point subdirectories), `single` (flat), or `unknown` (nothing generated) |
| `is_form_configmap(stem)` | Is this sibling `*.json` a setup form, or the DAG `manifest` / a `{atlan,csa}-connectors-*` credential template? |
| `form_configmap(dir, ep)` | Which file is *this* entry point's setup form, given the layout |

Everything that needs one of those reads it from there: the configmap
endpoint's default-entrypoint fallback (`application_sdk.handler.service`), the
artifact-schema registration guard (`_artifact_schema_guard`), and the
tenant-side setup-route check (`application_sdk.testing.setup_routes`).

The exclusion in `is_form_configmap` is load-bearing rather than cosmetic. For
a flat app, `atlan-connectors-<source>.json` sorts alphabetically **before**
`<source>.json`, so any file-discovery that globs `*.json` and takes the first
match picks the credential template on every single-entry-point connector.

Layout is read from the tree, never from `len(entry_points)`. A
**route/card-split** app has several `@entrypoint`s behind one marketplace card
and therefore one *flat* generated tree, so counting Python entry points calls
it a bundle and sends every consumer looking under a subdirectory the toolkit
will never write for it.

### The setup page resolves on two independent lookups

`/workflows/setup/<id>` in the UI spends its `id` segment twice: once to find
the marketplace card (`app.id == id`) and once to fetch the form
(`GET /api/service/configmaps/<id>`, which Heracles proxies to this SDK's
`GET /workflows/v1/configmap/{id}`). The route therefore resolves **only when
the card's `id` is a name the configmap endpoint also answers to**.

Those two names come from different places, and a contract change can move one
without the other — setting `Entrypoint.packageId` moves the card's identity
onto the marketplace package while the form stays under the workflow config's
`id`, which 404s the setup page with every generated artifact still
self-consistent and every static gate still green. `application_sdk.testing.setup_routes`
asserts that join against a live tenant; see
`docs/standards/connector-ci-e2e.md` for where it runs in CI.

### Per-entry-point handler & core modules

A multi-entry-point app can ship **per-entry-point** lifecycle code next to its hand-written package, discovered by convention:

```
app/
  asset_export_advanced/        # snake_case package
    handler.py                  # async test_auth / preflight_check / fetch_metadata
    core.py                     # compute_manifest (see Apps — Dynamic manifest)
```

The mapping between the kebab-case entry-point name (on the wire, and the `app/generated/<name>/` contract dir) and the snake_case Python package is the single canonical conversion `entrypoint_module_segment(name)` (`asset-export-advanced → asset_export_advanced`) — `application_sdk.handler.service` and entry-point registration both route through it, so the two never drift.

- **`app.<segment>.handler`** — optional `async def test_auth(input, ctx)`, `preflight_check`, `fetch_metadata`. When a request to `/workflows/v1/{auth,check,metadata}` carries an `entrypoint` (the bare name, resolved by the orchestrator from the marketplace catalog), the SDK dispatches to this module **by exact name**. See [Handlers — Per-entry-point handlers](handlers.md#per-entry-point-handlers).
- **`app.<segment>.core.compute_manifest`** — optional dynamic-manifest hook. See [Apps — Dynamic manifest](apps.md#dynamic-manifest-compute_manifest).

Discovery is best-effort and conservative: a missing module / wrong-shaped attribute falls through to the app-level `Handler` (1:1 with single-entry-point behaviour). Both the handler functions and `compute_manifest` must be `async def` — a sync `def` is ignored and falls through. A module that *exists but fails to import* (a real bug in the connector's code) is **not** swallowed — it surfaces.

### Dockerfile

One App = one `ATLAN_APP_MODULE` entry (no comma-separated list):

```dockerfile
ENV ATLAN_APP_MODULE=app.connector:SnowflakeApp
```

### Testing each entry point

Adding an entry point adds a workflow nothing tests by default. Both test tiers
need it named explicitly, because in both cases the silent fallback is a *pass*
against the wrong workflow rather than an error:

- **Integration** — set `entrypoint=` on the `Scenario`, or `entrypoint` on the
  suite class. In-process tests pass `entry_point=` to the executor. See
  [Integration testing — Naming the entrypoint](../guides/integration-testing.md#naming-the-entrypoint-on-a-multi-entrypoint-app).
- **e2e** — one `tests/e2e/test_*.py` file per entry point (the CI matrix fans out
  one leg per file), each subclassing that entry point's generated
  `app/generated/<ep>/_e2e_base.py`. Conformance rule `T025` reports any bundle
  entry point without one. See
  [Connector CI e2e — Multi-entrypoint apps](../standards/connector-ci-e2e.md#multi-entrypoint-bundle-apps-one-suite-per-entrypoint).
