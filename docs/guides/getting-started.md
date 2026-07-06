# Getting Started

This guide walks you through building and running your first application on the Atlan Application SDK — from an empty directory to a workflow visible in the Temporal UI.

---

## Prerequisites

You need just two tools installed:

| Tool | Minimum version | Install |
|------|----------------|---------|
| Python | 3.11 | [python.org](https://www.python.org/downloads/) |
| uv | latest | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |

The SDK auto-downloads and manages everything else — the Dapr runtime (`daprd`) and an in-process Temporal server — the first time you run an app locally. You do **not** need to install the Dapr or Temporal CLIs.

For platform-specific setup see the [macOS](../setup/MAC.md), [Linux](../setup/LINUX.md), or [Windows](../setup/WINDOWS.md) setup guides.

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

## Step 3: Run your app

For local development, `run_dev_combined()` runs the handler and worker in one process and boots the infrastructure for you — an **embedded Dapr runtime** and an **in-process Temporal server**, both auto-downloaded and managed by the SDK. Nothing else needs to be installed or running.

Create `run_dev.py`:

```python
import asyncio
from application_sdk.main import run_dev_combined
from app import HelloApp

asyncio.run(
    run_dev_combined(
        HelloApp,
        example_input={"name": "Atlan"},
        temporal_ui=True,  # serve the Temporal Web UI at http://localhost:8233
    )
)
```

Run it:

```bash
uv run python run_dev.py
```

The SDK starts:
- A **handler** (FastAPI) on `http://localhost:8000`
- A **worker** connected to the in-process Temporal server
- An **embedded Dapr sidecar** for state, secrets, and object storage

On first run it downloads `daprd` (the version pinned by the SDK) and the Temporal server into a local cache (`~/.cache/atlan-sdk/`); later runs reuse them. Because `example_input` is supplied, a workflow is kicked off automatically on startup.

---

## Step 4: Inspect — and re-trigger — the workflow

Open [http://localhost:8233](http://localhost:8233) in your browser to see the completed workflow in the embedded Temporal UI.

To trigger another run via the HTTP API:

```bash
curl -s -X POST http://localhost:8000/workflows/v1/start \
  -H "Content-Type: application/json" \
  -d '{"name": "Atlan"}'
```

> **Multi-entrypoint apps:** if your `App` has multiple `@entrypoint` methods, append `?entrypoint=<name>` to select one — e.g. `POST /workflows/v1/start?entrypoint=extract-metadata`. Apps with more than one entry point return HTTP 400 if the parameter is omitted. See [HTTP API reference](../reference/http-api.md).

---

## Optional: run against external Dapr + Temporal

The embedded runtime above is the recommended local-dev path. If you want to mirror production more closely — where the app runs against a Dapr **sidecar** and a standalone **Temporal cluster** — you only need the [Temporal CLI](https://docs.temporal.io/cli#install). You do **not** need the Dapr CLI: production runs the `daprd` runtime directly (see the SDK container entrypoint), and the SDK can fetch the same pinned `daprd` binary for you. Then:

**1. Add the SDK's Dapr component definitions to your project:**

The component YAMLs ship inside the `atlan-application-sdk` wheel — copy them out of your
installed dependency rather than downloading from GitHub (unauthenticated GitHub requests hit
rate limits under CI concurrency and can drift from the SDK version actually locked in your
`uv.lock`). This uses `poe` from the `poethepoet` package, so add it as a dependency first:

```bash
uv add poethepoet
```

Then add a `download-components` poe task to `pyproject.toml`:

```toml
[tool.poe.tasks]
download-components.shell = """
python -c "
import application_sdk, pathlib, shutil
src = pathlib.Path(application_sdk.__file__).parent / 'components'
shutil.copytree(src, 'components', dirs_exist_ok=True)
"
"""
```

Then run it:

```bash
uv run poe download-components
```

**2. Start Temporal and a Dapr sidecar in separate terminals:**

```bash
temporal server start-dev --db-filename temporal.db
```

```bash
# Resolve the pinned daprd binary via the SDK (downloads it on first use;
# no Dapr CLI / `dapr init` required), then run daprd directly:
DAPRD=$(uv run python -c "from application_sdk.dev._dapr import _ensure_daprd_binary; print(_ensure_daprd_binary())")

"$DAPRD" \
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

**3. Run the app in combined mode against that sidecar:**

```bash
DAPR_HTTP_PORT=3500 DAPR_GRPC_PORT=50001 \
  uv run application-sdk --mode combined --app app:HelloApp
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
