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

The base image (`registry.atlan.com/public/app-runtime-base:refactor-v3-latest`) includes the `application-sdk` CLI, Dapr, and the entrypoint. You do not need a custom `ENTRYPOINT`, `CMD`, or `entrypoint.sh`. The base image handles mode selection at runtime.

```dockerfile
# Application-sdk v3 base image (Chainguard-based)
FROM registry.atlan.com/public/app-runtime-base:refactor-v3-latest

WORKDIR /app

# Install dependencies first (better caching)
COPY --chown=appuser:appuser pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000,gid=1000 \
    uv venv .venv && \
    uv sync --locked --no-install-project

# Copy application code
COPY --chown=appuser:appuser . .

# App-specific environment variables
ENV ATLAN_APP_HTTP_PORT=8000
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
from my_package.handlers import MyHandler

asyncio.run(run_dev_combined(MyExtractor, handler_class=MyHandler))
```

This starts both the Temporal worker and the HTTP handler service in a single process. It derives the module path automatically from the class.

### Custom Secrets for Local Dev

Pass mock infrastructure for local development without a Dapr sidecar:

```python
from application_sdk.testing.mocks import MockSecretStore, MockStateStore
from application_sdk.main import run_dev_combined

asyncio.run(run_dev_combined(
    MyExtractor,
    handler_class=MyHandler,
    secret_store=MockSecretStore({"my-api-key": "dev-secret"}),
    state_store=MockStateStore(),
))
```

## Worker Auto-Discovery

You no longer register workflow or activity classes explicitly. The worker discovers everything at startup:

1. When Python imports your `App` subclass, `App.__init_subclass__` registers it in `AppRegistry` and its `@task` methods in `TaskRegistry`.
2. `create_worker()` reads both registries and configures the Temporal worker automatically.

If you need a worker handle directly (for integration tests):

```python
from application_sdk.execution import create_worker
from application_sdk.execution._temporal.backend import create_temporal_client

client = await create_temporal_client()  # reads TEMPORAL_HOST, TEMPORAL_NAMESPACE, etc.
worker = await create_worker(client)     # discovers all App subclasses automatically
await worker.run()
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ATLAN_APP_MODULE` | Yes (production) | Python module path, e.g. `app.app:MyExtractor` |
| `ATLAN_CONTRACT_GENERATED_DIR` | Recommended | Path to generated contract JSON files |
| `TEMPORAL_HOST` | Yes | Temporal server host |
| `TEMPORAL_NAMESPACE` | Yes | Temporal namespace |
