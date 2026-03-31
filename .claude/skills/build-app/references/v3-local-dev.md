# V3 Local Development — Quick Reference

## Prerequisites

- Python 3.11+
- `uv` package manager
- Temporal server (for workflow execution)
- Dapr (optional — SDK falls back to in-memory stores without it)

## Starting Local Infrastructure

### Temporal Dev Server

```bash
# Install Temporal CLI (macOS)
brew install temporal

# Start dev server
temporal server start-dev --ui-port 8233 --port 7233
```

Temporal UI: http://localhost:8233

### Dapr (Optional)

Without Dapr, the SDK uses in-memory state/secrets and local filesystem for storage. This is fine for development.

If you need Dapr:
```bash
# Install Dapr CLI
brew install dapr/tap/dapr-cli

# Initialize Dapr
dapr init

# Verify
dapr --version
```

## Running Your App

### Entry Point

```python
# main.py
import asyncio
from application_sdk.main import run_dev_combined
from app.my_app import MyApp
from app.handler import MyHandler

if __name__ == "__main__":
    asyncio.run(run_dev_combined(MyApp, handler_class=MyHandler))
```

### Start

```bash
cd <your-app-directory>
uv run python main.py
```

Wait for: `INFO: Uvicorn running on http://127.0.0.1:8000`

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOG_LEVEL` | `INFO` | Log verbosity (DEBUG, INFO, WARNING, ERROR) |
| `ATLAN_TEMPORARY_PATH` | `./local/tmp/` | Where temp files are written |
| `DAPR_HTTP_PORT` | (none) | Set to `3500` if running Dapr manually |
| `ATLAN_WORKFLOW_HOST` | `localhost` | Temporal host |
| `ATLAN_WORKFLOW_PORT` | `7233` | Temporal port |
| `ATLAN_WORKFLOW_NAMESPACE` | `default` | Temporal namespace |

## Testing Handler Endpoints

### Auth
```bash
curl -s -X POST http://localhost:8000/workflows/v1/auth \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": [
      {"key": "host", "value": "your-test-host"},
      {"key": "api_key", "value": "your-test-key"}
    ]
  }' | python -m json.tool
```

### Preflight
```bash
curl -s -X POST http://localhost:8000/workflows/v1/check \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": [
      {"key": "host", "value": "your-test-host"},
      {"key": "api_key", "value": "your-test-key"}
    ]
  }' | python -m json.tool
```

### Metadata
```bash
curl -s -X POST http://localhost:8000/workflows/v1/metadata \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": [
      {"key": "host", "value": "your-test-host"},
      {"key": "api_key", "value": "your-test-key"}
    ]
  }' | python -m json.tool
```

## Triggering a Workflow

### Start
```bash
curl -s -X POST http://localhost:8000/workflows/v1/start \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": [
      {"key": "host", "value": "your-test-host"},
      {"key": "api_key", "value": "your-test-key"}
    ],
    "metadata": {},
    "connection": {"connection": "dev"}
  }' | python -m json.tool
```

### Check Status
```bash
curl -s http://localhost:8000/workflows/v1/status/<workflow_id>/<run_id> | python -m json.tool
```

### Monitor in Temporal UI
Open http://localhost:8233, find your workflow by ID. The event history shows each @task call as a Temporal activity.

## Troubleshooting

### "Connection refused" on port 7233
Temporal server isn't running. Start it with `temporal server start-dev`.

### Handler returns 500
Check the app terminal for the traceback. Common causes:
- Handler class not discovered — import it in your App module
- Credential keys don't match what handler expects — check the key names

### Workflow starts but tasks fail
- Check Temporal UI for the activity failure details
- Common: timeout too short, missing API credentials, network errors

### "App context is only available during run() execution"
You're accessing `self.context` outside of a @task method. Move the access into a @task.

### Dapr not detected (fallback to in-memory)
The SDK checks `DAPR_HTTP_PORT` env var. If running manually (not via `atlan app run`), export it:
```bash
export DAPR_HTTP_PORT=3500
uv run python main.py
```

### Output files not appearing
Check `ATLAN_TEMPORARY_PATH` — defaults to `./local/tmp/`. If your tasks write to a different path, files may be elsewhere.
