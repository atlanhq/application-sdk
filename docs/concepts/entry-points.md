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

Behaviour:
- Returns 400 if `<entry-point-name>` fails validation (`^[a-zA-Z][a-zA-Z0-9_-]*$`).
- Returns 404 if the subfolder or `manifest.json` is missing.
- The `?entrypoint=` token is the same kebab-case identifier used on `POST /workflows/v1/start` — one naming convention, two endpoints.

For single-entry-point apps, `GET /workflows/v1/manifest` (no query param) is unchanged.

> **Configmap discovery** also benefits from the subfolder layout: the handler uses `rglob("*.json")` so configmap files can live inside per-entry subfolders alongside the manifests.

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
