# Build Your First Connector

This tutorial builds a complete, working connector from scratch. By the end you'll have:

- Typed input/output contracts
- A credential type with `test_auth`
- A task that fetches data page-by-page and writes JSONL output
- Local dev via `run_dev_combined`
- A unit test that runs without Dapr or Temporal

The connector calls a REST API and loads the results into Atlan as metadata entities. If you haven't done the [Getting Started](getting-started.md) walkthrough yet, do that first — it covers tool installation and the basic project layout.

---

## 1. Create the project

```bash
uv init github-connector
cd github-connector
uv add "atlan-application-sdk" httpx
mkdir app
touch app/__init__.py
```

---

## 2. Define contracts

Contracts are the typed boundaries between the SDK framework and your connector code. Every piece of data that crosses a task boundary must be declared here.

Create `app/contracts.py`:

```python
from __future__ import annotations

from application_sdk.contracts.base import Input, Output


# --- Workflow contracts ---

class RepoFetchInput(Input):
    credential_guid: str = ""
    org: str = ""
    output_path: str = ""
    max_pages: int = 10


class RepoFetchOutput(Output):
    output_path: str = ""
    record_count: int = 0
```

### What these types do

| Type | Purpose |
|---|---|
| `RepoFetchInput` | Passed to the `@task`; everything the task needs to do its work |
| `RepoFetchOutput` | Returned from the `@task`; tells the caller where output landed and how many records |

`Input` and `Output` are Pydantic `BaseModel` subclasses. Never use `@dataclass` on them — Pydantic handles construction, validation, and Temporal serialization. Field defaults matter: every field must have a default for schema evolution (adding a field to a deployed connector can't break existing callers).

---

## 3. Write the handler

The handler answers the SDK's pre-flight questions: can we connect? do the credentials work?

Create `app/handler.py`:

```python
from __future__ import annotations

import httpx

from application_sdk.handler import Handler
from application_sdk.handler.contracts import (
    ApiMetadataOutput,
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)


class GitHubHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        # Credentials arrive as a list of key/value pairs from Atlan.
        token = next((c.value for c in input.credentials if c.key == "token"), "")
        org = next((c.value for c in input.credentials if c.key == "org"), "")

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ) as client:
            resp = await client.get(f"https://api.github.com/orgs/{org}/repos")
            resp.raise_for_status()

        return AuthOutput(status=AuthStatus.SUCCESS, message="Authentication successful")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="OK")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        # Metadata extraction is driven by the App, not the handler.
        return ApiMetadataOutput()
```

The handler receives credentials as `input.credentials: list[HandlerCredential]` — a list of opaque key/value pairs sent by Atlan. Extract the fields you need by key name.

---

## 4. Write the connector

The connector is an `App` subclass. `run()` orchestrates tasks; tasks do the actual work.

Create `app/connector.py`:

```python
from __future__ import annotations

import json
import os
import tempfile

import httpx

from application_sdk.app import App, task
from application_sdk.credentials import CredentialResolver, bearer_token_ref
from application_sdk.infrastructure import get_infrastructure
from application_sdk.storage import upload_file

from app.contracts import RepoFetchInput, RepoFetchOutput


class GitHubConnector(App):

    async def run(self, input: RepoFetchInput) -> RepoFetchOutput:
        return await self.fetch_repos(input)

    @task(timeout_seconds=3600, auto_heartbeat_seconds=30)
    async def fetch_repos(self, input: RepoFetchInput) -> RepoFetchOutput:
        # Resolve credentials from the secret store by name.
        infra = get_infrastructure()
        resolver = CredentialResolver(secret_store=infra.secret_store)
        ref = bearer_token_ref(input.credential_guid)
        raw = await resolver.resolve_raw(ref)
        token = raw.get("token", "")
        org = raw.get("org", input.org)

        output_file = f"artifacts/github-connector/{input.credential_guid}/repos.jsonl"
        record_count = 0

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
            tmp_path = tmp.name
            try:
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30,
                ) as client:
                    for page in range(1, input.max_pages + 1):
                        resp = await client.get(
                            f"https://api.github.com/orgs/{org}/repos",
                            params={"page": page, "per_page": 100},
                        )
                        resp.raise_for_status()
                        repos = resp.json()
                        if not repos:
                            break

                        for repo in repos:
                            entity = {
                                "typeName": "Schema",
                                "attributes": {
                                    "name": repo["name"],
                                    "qualifiedName": f"github/{org}/{repo['name']}",
                                    "description": repo.get("description") or "",
                                },
                            }
                            tmp.write(json.dumps(entity) + "\n")
                            record_count += 1
            finally:
                pass  # tmp file written, upload below

        await upload_file(output_file, tmp_path)
        os.unlink(tmp_path)

        return RepoFetchOutput(output_path=output_file, record_count=record_count)
```

### Key patterns

**`@task(timeout_seconds=3600, auto_heartbeat_seconds=30)`** — the `auto_heartbeat_seconds` parameter sends a Temporal heartbeat every 30 seconds automatically. Without heartbeats, Temporal marks a long-running task as timed out and retries it. You don't need to call `heartbeat()` manually when `auto_heartbeat_seconds` is set.

**Write to a temp file, then `upload_file`** — tasks run inside the Temporal worker process. Don't accumulate large lists in memory; write to a local temp file and upload it to object storage. The framework guarantees the upload path is accessible to downstream tasks.

**Resolve credentials inside the task, not in `run()`** — tasks may be retried on a different worker pod. Resolve credentials at the top of each task so every retry gets a fresh, valid credential. Use `bearer_token_ref(name)` + `CredentialResolver.resolve_raw()` for the named-credential path (works with `MockSecretStore` in tests).

---

## 5. Run it locally

Create `run_dev.py`:

```python
import asyncio
from application_sdk.main import run_dev_combined
from app.connector import GitHubConnector

asyncio.run(
    run_dev_combined(
        GitHubConnector,
        credentials={
            "authType": "bearer",
            "token": "ghp_your_personal_access_token",
            "org": "your-github-org",
        },
        example_input={
            "credential_guid": "test-credential",
            "org": "your-github-org",
            "max_pages": 2,
        },
    )
)
```

Copy the Dapr component definitions into your project. The components ship with the SDK repo, not the wheel:

```bash
# If you have the SDK repo cloned locally:
cp -r /path/to/application-sdk/components ./components

# Otherwise, download them directly:
curl -sL https://github.com/atlanhq/application-sdk/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=2 application-sdk-main/components
```

Start infrastructure and run:

```bash
# Terminal 1
temporal server start-dev --db-filename temporal.db

# Terminal 2
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

# Terminal 3
DAPR_HTTP_PORT=3500 DAPR_GRPC_PORT=50001 uv run python run_dev.py
```

After a few seconds, open [http://localhost:8233](http://localhost:8233) to see the workflow. Look for a `repos.jsonl` file in your local object store (`./local/dapr/objectstore/`).

---

## 6. Test without Dapr or Temporal

Unit tests inject a `MockSecretStore` so no real credential store is needed. Use `pytest-httpx` to mock the GitHub API.

```bash
uv add --dev pytest pytest-asyncio pytest-httpx
```

Create `tests/test_connector.py`:

```python
import json
import pytest
from application_sdk.infrastructure import set_infrastructure, clear_infrastructure
from application_sdk.infrastructure.context import InfrastructureContext
from application_sdk.testing import MockSecretStore
from app.connector import GitHubConnector
from app.contracts import RepoFetchInput


@pytest.fixture(autouse=True)
def infra():
    # MockSecretStore stores secrets as JSON strings keyed by credential name.
    set_infrastructure(
        InfrastructureContext(
            secret_store=MockSecretStore({
                "test-cred": json.dumps({
                    "type": "bearer_token",
                    "token": "fake-token",
                })
            })
        )
    )
    yield
    clear_infrastructure()


@pytest.mark.asyncio
async def test_fetch_repos(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="https://api.github.com/orgs/test-org/repos?page=1&per_page=100",
        json=[
            {"name": "repo-a", "description": "First repo"},
            {"name": "repo-b", "description": "Second repo"},
        ],
    )
    httpx_mock.add_response(
        url="https://api.github.com/orgs/test-org/repos?page=2&per_page=100",
        json=[],  # empty page — signals end of pagination
    )

    connector = GitHubConnector()
    result = await connector.fetch_repos(
        RepoFetchInput(
            credential_guid="test-cred",
            org="test-org",
            max_pages=5,
        )
    )

    assert result.record_count == 2
```

Run:

```bash
uv run pytest tests/ -v
```

### What the test demonstrates

- **`MockSecretStore`** intercepts credential resolution without any Dapr sidecar. The dict maps credential names to JSON strings, which `CredentialResolver` parses the same way it would parse a real secret-store response.
- **`set_infrastructure` / `clear_infrastructure`** swap the infrastructure context in and out per test. Always call `clear_infrastructure()` in teardown (the `yield` fixture pattern handles this).
- The task is called directly as a coroutine (`await connector.fetch_repos(...)`) — no Temporal worker needed.

---

## What to build next

You now have the full skeleton of a production-grade connector. The most common additions from here:

| Next step | Where to look |
|---|---|
| Add a second workflow (e.g. lineage extraction) | [Entry Points](../concepts/entry-points.md) |
| Pass large datasets between tasks safely | [Storage](../concepts/storage.md) — `FileReference`, `StorageTier` |
| Use a SQL database instead of REST | [SQL Application Guide](sql-application-guide.md) |
| Generate the setup form and AE DAG | [PKL Contracts](pkl-contracts.md) |
| Deploy to Kubernetes | [Deployment](deployment.md) |
| Expose tasks to AI assistants | [MCP](../concepts/mcp.md) |
