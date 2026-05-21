# CLI Reference

The `application-sdk` CLI is the production entry point for running apps. It is installed as a console script when you add `atlan-application-sdk` as a dependency.

```bash
application-sdk [options]
# or
python -m application_sdk.main [options]
```

---

## Synopsis

```
application-sdk --mode {worker,handler,combined} --app MODULE:CLASS [options]
```

---

## Required Arguments

| Flag | Short | Description |
|------|-------|-------------|
| `--app` | `-a` | App class path in `module:ClassName` form (e.g. `app.connector:PostgresApp`). Apps expose multiple workflows via `@entrypoint` methods. Also accepts `ATLAN_APP_MODULE` env var. Required — enforced by config validation (not by argparse), so `--help` will not mark it as required. |

---

## Worker Options

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `--temporal-host` | `ATLAN_TEMPORAL_HOST` | `localhost:7233` | Temporal server address (`host:port`) |
| `--temporal-namespace` | `ATLAN_TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `--task-queue` | `ATLAN_TASK_QUEUE` | _(derived from app)_ | Task queue name |

---

## Handler Options

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `--handler-host` / `--host` | `ATLAN_HANDLER_HOST` | `0.0.0.0` | Handler bind host |
| `--handler-port` / `--port` | `ATLAN_HANDLER_PORT` | `8000` | Handler bind port |
| `--health-port` | `ATLAN_HEALTH_PORT` | `8081` | Worker health check port |
| `--handler` | `ATLAN_HANDLER_MODULE` | _(auto-discovered)_ | Custom handler class path |

---

## Common Options

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `--mode` | `ATLAN_APP_MODE` | `combined` | Execution mode: `worker`, `handler`, or `combined`. Falls back to `APPLICATION_MODE` (v2 legacy). Default is resolved by `AppConfig`, so `--help` shows no default. |
| `--log-level` | `ATLAN_LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. (`CRITICAL` is not a recommended level per ADR-0011.) |
| `--service-name` | `ATLAN_SERVICE_NAME`, `OTEL_SERVICE_NAME` | _(derived from app)_ | Service name for observability |

---

## Modes

### `worker`

Starts only the Temporal worker. Used in the worker Kubernetes Deployment. Scales to zero via KEDA when the task queue is empty.

```bash
application-sdk --mode worker --app app.connector:PostgresApp
```

### `handler`

Starts only the FastAPI HTTP handler. Used in the handler Kubernetes Deployment. Always-on (min 1 replica).

```bash
application-sdk --mode handler --app app.connector:PostgresApp --port 8000
```

### `combined`

Starts both handler and worker in a single process. Used for local development and very small apps.

```bash
application-sdk --mode combined --app app.connector:PostgresApp
```

---

## Examples

```bash
# Local development with debug logging
application-sdk --mode combined --app app.connector:PostgresApp --log-level DEBUG

# Production worker pointing at remote Temporal
ATLAN_TEMPORAL_HOST=temporal.internal:7233 \
ATLAN_AUTH_ENABLED=true \
  application-sdk --mode worker --app app.connector:PostgresApp

# Handler on a non-default port
application-sdk --mode handler --app app.connector:PostgresApp --port 9000

# Combined with a custom task queue
application-sdk --mode combined --app app.connector:PostgresApp \
  --task-queue postgres-custom-queue
```

---

## Environment Variable Fallbacks

The CLI accepts both explicit flags and environment variables. Flags take priority over env vars. The priority order for key settings:

| Setting | Priority 1 | Priority 2 | Priority 3 | Default |
|---------|-----------|-----------|-----------|---------|
| Temporal host | `--temporal-host` | `ATLAN_TEMPORAL_HOST` | `ATLAN_WORKFLOW_HOST` (v2) | `localhost:7233` |
| Handler host | `--handler-host` | `ATLAN_HANDLER_HOST` | `ATLAN_APP_HTTP_HOST` (v2) | `0.0.0.0` |
| Log level | `--log-level` | `ATLAN_LOG_LEVEL` | `LOG_LEVEL` (v2) | `INFO` |

---

## Programmatic Alternative: `run_dev_combined`

For local development scripts, `run_dev_combined()` is more ergonomic than the CLI:

```python
import asyncio
from application_sdk.main import run_dev_combined
from app.connector import PostgresApp

asyncio.run(
    run_dev_combined(
        PostgresApp,
        credentials={"host": "localhost", "port": "5432", ...},
        example_input={"connection": {"connection_name": "test", ...}},
        temporal_ui=True,
        temporal_ui_port=8233,
    )
)
```

`temporal_ui` is local-dev only. When enabled, the embedded Temporal Web UI
binds to `http://127.0.0.1:8233` unless `temporal_ui_port` is set.

See [Getting Started](../guides/getting-started.md#step-7-optional-use-run_dev_combined) for details.
