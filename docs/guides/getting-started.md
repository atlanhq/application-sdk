# Getting Started

This guide walks you through building and running your first application on the Atlan Application SDK — from an empty directory to a workflow visible in the Temporal UI.

---

## Prerequisites

You need the following tools installed:

| Tool | Minimum version | Install |
|------|----------------|---------|
| Python | 3.11 | [python.org](https://www.python.org/downloads/) |
| uv | latest | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |
| Temporal CLI | latest | [docs.temporal.io/cli](https://docs.temporal.io/cli#install) |
| Dapr CLI | 1.14+ | [docs.dapr.io/getting-started/install-dapr-cli/](https://docs.dapr.io/getting-started/install-dapr-cli/) |

For platform-specific setup instructions see the [macOS](../setup/MAC.md), [Linux](../setup/LINUX.md), or [Windows](../setup/WINDOWS.md) setup guides.

---

## Step 1: Create a new project

```bash
uv init my-app
cd my-app
uv add atlan-application-sdk
```

---

## Step 2: Write your first app

Create `app.py`:

```python
from application_sdk.app import App, task
from application_sdk.contracts import Input, Output


class HelloInput(Input):
    name: str = "world"


class HelloOutput(Output):
    message: str = ""


class HelloApp(App):
    @task(timeout_seconds=60)
    async def greet(self, input: HelloInput) -> HelloOutput:
        msg = f"Hello, {input.name}!"
        self.logger.info("greeting name=%s", input.name)
        return HelloOutput(message=msg)

    async def run(self, input: HelloInput) -> HelloOutput:
        return await self.greet(input)
```

---

## Step 3: Add Dapr components

The SDK uses Dapr for state, secrets, and pub/sub. Copy the ready-made component definitions into your project:

```bash
# If you have the SDK repo cloned locally:
cp -r /path/to/application-sdk/components ./components

# Otherwise, download them directly:
curl -sL https://github.com/atlanhq/application-sdk/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=2 application-sdk-main/components
```

Your project should now look like:

```
my-app/
├── app.py
├── components/       # Dapr component YAMLs
├── pyproject.toml
└── .python-version
```

---

## Step 4: Start local infrastructure

Open two terminal tabs.

**Tab 1 — Temporal:**
```bash
temporal server start-dev --db-filename temporal.db
```

**Tab 2 — Dapr sidecar:**
```bash
dapr run \
  --app-id app \
  --app-port 8000 \
  --dapr-http-port 3500 \
  --dapr-grpc-port 50001 \
  --scheduler-host-address '' \
  --placement-host-address '' \
  --max-body-size 1024Mi \
  --config components/configuration.yaml \
  --resources-path components
```

---

## Step 5: Run your app

Back in your project directory, run the combined handler + worker:

```bash
DAPR_HTTP_PORT=3500 DAPR_GRPC_PORT=50001 \
  uv run application-sdk --mode combined --app app:HelloApp
```

The SDK starts:
- A **handler** (FastAPI) on `http://localhost:8000`
- A **worker** connected to Temporal on `localhost:7233`

---

## Step 6: Trigger a workflow

In a new terminal, send a workflow start request:

```bash
curl -s -X POST http://localhost:8000/workflows/v1/start \
  -H "Content-Type: application/json" \
  -d '{"name": "Atlan"}'
```

Then open [http://localhost:8233](http://localhost:8233) in your browser to see the completed workflow in the Temporal UI.

---

## Step 7: (optional) Use run_dev_combined

For iterative local development, `run_dev_combined()` is more convenient than the CLI. It starts the handler and worker in one process, auto-provisions credentials, and kicks off a workflow automatically.

Create `run_dev.py`:

```python
import asyncio
from application_sdk.main import run_dev_combined
from app import HelloApp

asyncio.run(
    run_dev_combined(
        HelloApp,
        example_input={"name": "Atlan"},
    )
)
```

Run with Dapr sidecar already started (Step 4):

```bash
DAPR_HTTP_PORT=3500 DAPR_GRPC_PORT=50001 uv run python run_dev.py
```

---

## Next steps

- **Build a real connector** → [Build Your First Connector](./build-your-first-app.md)
- **Build a SQL connector** → [SQL Application Guide](./sql-application-guide.md)
- **Understand the architecture** → [Architecture](./architecture.md)
- **Deep-dive on Apps and tasks** → [Apps](../concepts/apps.md) · [Tasks](../concepts/tasks.md)
- **Configuration reference** → [Configuration](../configuration.md)
- **Integration testing** → [Integration Testing Guide](./integration-testing.md)
- **More end-to-end examples** → [atlan-sample-apps](https://github.com/atlanhq/atlan-sample-apps)
