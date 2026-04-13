# Server (Handler Service)

In v3, the HTTP server is created by the `create_app_handler_service()` function. This replaces the v2 `APIServer` class and its `register_workflow()` / `HttpWorkflowTrigger` pattern.

## Creating the Handler Service

The handler service auto-wires routes from your `Handler` methods:

```python
from application_sdk.handler import create_app_handler_service
from my_package.handlers import MyHandler

app = create_app_handler_service(handler_class=MyHandler)
```

When started (via the CLI `--mode handler` or `--mode combined`), this creates a FastAPI application with:

- Routes for `test_auth`, `preflight_check`, and `fetch_metadata` derived from your `Handler` subclass
- Health check endpoints
- MCP server integration (if configured)

## Health Check Endpoints

Every handler service exposes:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/server/health` | GET | Liveness probe |
| `/server/ready` | GET | Readiness probe |

These are registered automatically. No configuration is needed.

## Handler Method Routing

The service maps your `Handler` methods to HTTP endpoints:

| Handler method | HTTP endpoint | Method |
|---------------|---------------|--------|
| `test_auth()` | `/workflows/v1/test_auth` | POST |
| `preflight_check()` | `/workflows/v1/preflight_check` | POST |
| `fetch_metadata()` | `/workflows/v1/metadata` | POST |

The service layer injects `self.context` into the handler before each request and clears it after. Your handler methods receive typed `Input` objects and return typed `Output` objects.

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
from my_package.handlers import MyHandler

import asyncio
asyncio.run(run_dev_combined(MyExtractor, handler_class=MyHandler))
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
