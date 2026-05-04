---
name: add-mcp-tool
description: >
  Expose existing @task methods as MCP tools. Adds @mcp_tool decorator,
  installs the mcp extra, sets ENABLE_MCP=true, and verifies the /mcp
  endpoint is reachable.
mandatory_triggers:
  - "/add-mcp-tool"
  - "add mcp tool"
  - "expose as mcp"
optional_triggers:
  - "make task available to ai"
  - "mcp integration"
  - "enable mcp"
owner: connector-platform-team
last_updated: "2026-04-30"
staleness_days: 90
inputs:
  - task_methods: list[str] — which @task methods to expose (or "all")
outputs:
  - Modified app/connector.py (@mcp_tool decorators added)
  - Modified pyproject.toml (mcp extra / dependency group)
  - Modified .env or .env.example (ENABLE_MCP=true)
gates: []
---

# add-mcp-tool

Expose one or more existing `@task` methods as [Model Context Protocol](https://spec.modelcontextprotocol.io/) tools.
Once enabled, AI assistants (Claude Desktop, Claude Code, Cursor) can discover and call your tasks directly.

## How MCP Works in the SDK

When `ENABLE_MCP=true`, the handler mounts an MCP server at `/mcp`. At startup,
it scans the `TaskRegistry` for methods decorated with `@mcp_tool` and registers
them as tools. Each tool's parameters are derived from the task's `Input` Pydantic
model — fields are automatically converted to JSON Schema.

## Steps

### 1. Install the `mcp` extra

```bash
uv add "atlan-application-sdk[mcp]"
```

Or if the project uses dependency groups:
```bash
uv add --group mcp "atlan-application-sdk[mcp]"
```

Verify `pyproject.toml` has the `mcp` dependency.

### 2. Add `@mcp_tool` to the chosen task methods

```python
from application_sdk.server.mcp.decorators import mcp_tool
from application_sdk.app import App, task

class MyConnector(App):
    @task(timeout_seconds=3600)
    @mcp_tool(
        name="fetch_metadata",
        description="Fetch database schemas and tables from the connected source",
    )
    async def fetch_metadata(self, input: FetchInput) -> FetchOutput:
        ...
```

**Order matters:** `@task` must be the outer decorator, `@mcp_tool` must be inner (closer to the function).

**Decorator parameters:**
- `name` (optional): Tool name shown to AI clients. Defaults to the function name.
- `description` (optional): Tool description. Defaults to the docstring.
- `visible` (bool, default `True`): Set `False` to register but hide from AI discovery.

### 3. Set `ENABLE_MCP=true`

Add to `.env` (local) and your Kubernetes `ConfigMap` (production):

```bash
ENABLE_MCP=true
```

### 4. Restart and verify

Start the app and check the MCP endpoint:

```bash
# Start the app
ENABLE_MCP=true uv run application-sdk --mode combined --app app.connector:MyApp

# List registered tools
curl http://localhost:8000/mcp
```

The response should list all `@mcp_tool`-decorated tasks.

### 5. Connect an AI client

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "my-connector": {
      "url": "http://localhost:8000/mcp",
      "transport": "http"
    }
  }
}
```

**Claude Code** — run in the project directory:
```bash
claude mcp add my-connector http://localhost:8000/mcp
```

## What Pydantic Models Look Like to AI Clients

Given:
```python
class FetchInput(Input):
    database: str
    schema_filter: str = ".*"
    max_tables: Annotated[int, Field(le=1000)] = 100
```

The MCP tool parameters become:
```json
{
  "database": { "type": "string", "required": true },
  "schema_filter": { "type": "string", "default": ".*" },
  "max_tables": { "type": "integer", "maximum": 1000, "default": 100 }
}
```

## Security Note

The MCP endpoint (`/mcp`) is unauthenticated by default. In production, ensure it is
behind a network policy or auth proxy that restricts access to trusted AI clients only.
Do not expose MCP to the public internet without authentication.

## Verification Checklist

- [ ] `uv run python -c "from application_sdk.server.mcp.decorators import mcp_tool"` — no import error
- [ ] `ENABLE_MCP=true` in environment
- [ ] `curl http://localhost:8000/mcp` returns tool list
- [ ] AI client can discover and call the tool
