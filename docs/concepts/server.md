# Server (Handler Service)

In v3, the HTTP server is created by the `create_app_handler_service()` function. This replaces the v2 `APIServer` class and its `register_workflow()` / `HttpWorkflowTrigger` pattern.

## Creating the Handler Service

The handler service auto-wires routes from your `Handler` methods:

```python
from application_sdk.handler import create_app_handler_service
from my_package.handlers import MyHandler

app = create_app_handler_service(handler=MyHandler())
```

When started (via the CLI `--mode handler` or `--mode combined`), this creates a FastAPI application with:

- Routes for `test_auth`, `preflight_check`, and `fetch_metadata` derived from your `Handler` subclass
- Health check endpoints
- MCP server integration (if configured)

## Health Check Endpoints

Every handler service exposes health checks at two equivalent paths each:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` (alias: `/server/health`) | GET | Liveness probe |
| `/ready` (alias: `/server/ready`) | GET | Readiness probe |

These are registered automatically. No configuration is needed.

## Handler Method Routing

The service maps your `Handler` methods to HTTP endpoints:

| Handler method | HTTP endpoint | Method |
|---------------|---------------|--------|
| `test_auth()` | `/workflows/v1/auth` | POST |
| `preflight_check()` | `/workflows/v1/check` | POST |
| `fetch_metadata()` | `/workflows/v1/metadata` | POST |

The service layer injects `self.context` into the handler before each request and clears it after. Your handler methods receive typed `Input` objects and return typed `Output` objects.

## Full Endpoint Reference

All routes registered by `create_app_handler_service()`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/workflows/v1/auth` | POST | Test authentication credentials |
| `/workflows/v1/check` | POST | Run preflight connectivity checks |
| `/workflows/v1/metadata` | POST | Browse source metadata (schemas, tables) |
| `/workflows/v1/start` | POST | Start a workflow run (use `?entrypoint=<name>` for multi-entry-point apps) |
| `/workflows/v1/stop/{workflow_id}/{run_id}` | POST | Terminate a running workflow |
| `/workflows/v1/result/{workflow_id}` | GET | Fetch the final result of a completed workflow |
| `/workflows/v1/status/{workflow_id}/{run_id}` | GET | Poll workflow execution status |
| `/workflows/v1/config/{config_id}` | GET | Retrieve a workflow config (configmap) |
| `/workflows/v1/config/{config_id}` | POST | Update a workflow config |
| `/workflows/v1/file` | POST | Upload a file to the handler |
| `/workflows/v1/configmap/{config_map_id}` | GET | Retrieve a configmap by ID |
| `/workflows/v1/configmaps` | GET | List all configmaps |
| `/workflows/v1/manifest` | GET | Retrieve the app manifest (supports `?entrypoint=<name>`) |
| `/manifest` | GET | Alias for `/workflows/v1/manifest` (for backward compat) |
| `/workflows/v1/dev/local-vault` | POST | Provision credentials in the local dev vault |
| `/dapr/subscribe` | GET | Dapr pub/sub subscription list |
| `/events/v1/event/{event_id}` | POST | Handle a Dapr event |
| `/events/v1/drop` | POST | Return a Dapr `DROP` status, instructing the sidecar to drop the received event without retry or dead-lettering |
| `/health`, `/server/health` | GET | Liveness probe |
| `/ready`, `/server/ready` | GET | Readiness probe |
| `/metrics` | GET | Prometheus metrics (enabled when `ATLAN_ENABLE_PROMETHEUS_METRICS=true`) |
| `/` | GET | API root / service info |

## MCP Server Integration

If your application exposes an MCP (Model Context Protocol) server, the handler service can integrate it. MCP endpoints are wired alongside the standard handler routes.

## CLI Usage

In production, run the handler service as a separate pod:

```bash
application-sdk --mode handler --app my_package.apps:MyExtractor
```

For local development, `--mode combined` runs both the handler service and the Temporal worker in one process:

```bash
application-sdk --mode combined --app my_package.apps:MyExtractor
```

## Programmatic Usage

For integration tests or custom setups:

```python
from application_sdk.main import run_dev_combined
from my_package.apps import MyExtractor

import asyncio
asyncio.run(run_dev_combined(MyExtractor))
```

## Error Handling

Handler methods signal errors by raising `HandlerError`:

```python
from application_sdk.handler import Handler, HandlerError

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        if not valid:
            raise HandlerError("Invalid credentials", http_status=401)
        ...
```

The service translates `HandlerError` into a structured HTTP error response with the specified status code.
