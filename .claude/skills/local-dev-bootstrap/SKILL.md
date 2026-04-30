---
name: local-dev-bootstrap
description: >
  One-shot local development bootstrap. Ensures Dapr + Temporal are running,
  generates .env from .env.example if absent, and runs the app in combined mode
  via run_dev_combined(). Diagnoses and fixes common startup failures.
mandatory_triggers:
  - "/local-dev-bootstrap"
  - "bootstrap local dev"
  - "set up local development"
optional_triggers:
  - "start local dev"
  - "get running locally"
  - "run the app locally"
owner: connector-platform-team
last_updated: "2026-04-30"
staleness_days: 60
inputs:
  - app_class_path: string — optional, e.g. "app.connector:MyApp" (auto-detected if absent)
outputs:
  - .env file (if absent)
  - Running Temporal + Dapr + app (in user's terminal)
gates: []
---

# local-dev-bootstrap

Get the app running locally in one shot. Checks prerequisites, starts
infrastructure, configures `.env`, and launches the app.

## Steps

### 1. Verify prerequisites

Check that each tool is installed:

```bash
python --version          # must be 3.11+
uv --version
temporal --version        # temporal CLI
dapr --version
```

If any are missing, point the user to the platform setup guide:
- [macOS](../../docs/setup/MAC.md)
- [Linux](../../docs/setup/LINUX.md)
- [Windows](../../docs/setup/WINDOWS.md)

### 2. Install Python dependencies

```bash
uv sync --all-extras --all-groups
```

### 3. Generate `.env` if absent

```bash
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo ".env created from .env.example — review and update values before running"
fi
```

If `.env.example` is also absent, generate a minimal `.env`:
```bash
cat > .env << 'EOF'
ATLAN_APP_MODULE=app.connector:MyApp
DAPR_HTTP_PORT=3500
DAPR_GRPC_PORT=50001
ATLAN_TEMPORAL_HOST=localhost:7233
ATLAN_LOG_LEVEL=DEBUG
EOF
```

### 4. Check / start Temporal

```bash
# Check if already running
if temporal operator cluster health --address localhost:7233 2>/dev/null; then
  echo "Temporal already running"
else
  echo "Starting Temporal dev server..."
  temporal server start-dev --db-filename temporal.db &
  sleep 3
fi
```

Temporal UI: http://localhost:8233

### 5. Check / start Dapr

```bash
# Check if Dapr sidecar is already running
if curl -s http://localhost:3500/v1.0/metadata > /dev/null 2>&1; then
  echo "Dapr already running"
else
  echo "Starting Dapr sidecar..."
  # Verify components/ directory exists
  if [ ! -d components ]; then
    echo "ERROR: components/ directory not found."
    echo "Copy from the SDK repo: cp -r /path/to/application-sdk/components ./components"
    exit 1
  fi

  dapr run \
    --app-id app \
    --app-port 8000 \
    --dapr-http-port 3500 \
    --dapr-grpc-port 50001 \
    --scheduler-host-address '' \
    --placement-host-address '' \
    --max-body-size 1024Mi \
    --config components/configuration.yaml \
    --resources-path components &

  sleep 5
fi
```

### 6. Detect the App class

If the user didn't specify `app_class_path`:
1. Check `ATLAN_APP_MODULE` in `.env`.
2. Search `app/connector.py` for `class.*App` that subclasses `App`.
3. Ask the user if ambiguous.

### 7. Run the app

**Option A — `run_dev_combined` (recommended):**

Check if `run_dev.py` exists. If so, run it:
```bash
DAPR_HTTP_PORT=3500 DAPR_GRPC_PORT=50001 uv run python run_dev.py
```

If `run_dev.py` doesn't exist, generate it:
```python
# run_dev.py
import asyncio
from application_sdk.main import run_dev_combined
from {module} import {ClassName}

asyncio.run(
    run_dev_combined(
        {ClassName},
        credentials={
            # TODO: fill in your test credentials
            "host": "localhost",
            "port": "5432",
            "authType": "basic",
            "username": "admin",
            "password": "secret",
            "extra": {"database": "mydb"},
        },
        example_input={
            "connection": {
                "connection_name": "local-test",
                "connection_qualified_name": "default/{app_name}/1234567890",
            },
        },
    )
)
```

Then run:
```bash
DAPR_HTTP_PORT=3500 DAPR_GRPC_PORT=50001 uv run python run_dev.py
```

**Option B — CLI:**
```bash
DAPR_HTTP_PORT=3500 DAPR_GRPC_PORT=50001 \
  uv run application-sdk --mode combined --app {app_class_path}
```

### 8. Verify

- Handler: `curl http://localhost:8000/health` → `{"status":"healthy"}`
- Temporal UI: http://localhost:8233 → verify the workflow appears

## Common Failures

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Connection refused :7233` | Temporal not running | Run `temporal server start-dev` in a separate terminal |
| `Connection refused :3500` | Dapr not running | Start Dapr sidecar (Step 5) |
| `No module named 'app'` | Not in the project root | `cd` to the project root before running |
| `ATLAN_APP_MODULE not set` | Missing env var | Set it in `.env` or pass `--app` flag |
| `components/ not found` | Missing Dapr components | Copy from the SDK repo |
| `ImportError: atlan_application_sdk` | SDK not installed | Run `uv sync` |

## Teardown

To stop all local processes cleanly:

```bash
# Stop the app (Ctrl-C in the terminal running run_dev.py)
# Stop Dapr
dapr stop --app-id app
# Stop Temporal (if started by this skill)
temporal server stop    # or pkill -f "temporal server"
```
