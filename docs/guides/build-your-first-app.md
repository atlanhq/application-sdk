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

from dataclasses import dataclass, field

from application_sdk.contracts.base import Input, Output
from application_sdk.credentials.types import BearerTokenCredential


# --- Credential type ---

class GitHubCredential(BearerTokenCredential):
    """GitHub personal access token credential.

    The `token` field comes from BearerTokenCredential and holds
    the raw PAT. We add `org` to scope requests to a single org.
    """
    org: str = ""


# --- Handler contracts ---

@dataclass
class RepoFetchInput(Input):
    credential_guid: str = ""
    output_path: str = ""
    max_pages: int = 10


@dataclass
class RepoFetchOutput(Output):
    output_path: str = ""
    record_count: int = 0
```

### What these types do

| Type | Purpose |
|---|---|
| `GitHubCredential` | Maps credential store fields to typed attributes; resolved from the secret store by GUID at runtime |
| `RepoFetchInput` | Passed to the `@task`; everything the task needs to do its work |
| `RepoFetchOutput` | Returned from the `@task`; tells the caller where output landed and how many records |

The `Input` and `Output` base classes are plain dataclasses. Field defaults matter: every field must have a default for schema evolution (adding a field to a deployed connector can't break existing callers).

---

## 3. Write the handler

The handler answers the SDK's pre-flight questions: can we connect? do the credentials work?

Create `app/handler.py`:

```python
from __future__ import annotations

import httpx

from application_sdk.credentials import resolve_credentials
from application_sdk.handler import Handler
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    PreflightInput,
    PreflightOutput,
)

from app.contracts import GitHubCredential


class GitHubHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        credential = await resolve_credentials(input.credential_guid, GitHubCredential)

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {credential.token}"},
            timeout=10,
        ) as client:
            resp = await client.get("https://api.github.com/user/orgs")
            resp.raise_for_status()

        return AuthOutput(success=True)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(success=True)
```

`resolve_credentials(guid, CredentialType)` looks up the raw credential dict from the secret store and returns a typed `GitHubCredential` instance. The handler never sees a raw `dict` — just typed fields.

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
from application_sdk.credentials import resolve_credentials
from application_sdk.storage import upload_file

from app.contracts import (
    GitHubCredential,
    RepoFetchInput,
    RepoFetchOutput,
)
from app.handler import GitHubHandler


class GitHubConnector(App):
    handler_class = GitHubHandler

    async def run(self, input: RepoFetchInput) -> RepoFetchOutput:
        return await self.fetch_repos(input)

    @task(timeout_seconds=3600, auto_heartbeat_seconds=30)
    async def fetch_repos(self, input: RepoFetchInput) -> RepoFetchOutput:
        credential = await resolve_credentials(input.credential_guid, GitHubCredential)

        output_file = os.path.join(input.output_path or ".", "repos.jsonl")
        record_count = 0

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
            tmp_path = tmp.name
            try:
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {credential.token}"},
                    timeout=30,
                ) as client:
                    for page in range(1, input.max_pages + 1):
                        resp = await client.get(
                            f"https://api.github.com/orgs/{credential.org}/repos",
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
                                    "qualifiedName": f"github/{credential.org}/{repo['name']}",
                                    "description": repo.get("description") or "",
                                },
                            }
                            tmp.write(json.dumps(entity) + "\n")
                            record_count += 1
            finally:
                pass  # tmp file written, upload below

        await upload_file(output_file, tmp_path)
        os.unlink(tmp_path)

        return RepoFetchOutput(output_path=input.output_path or ".", record_count=record_count)
```

### Key patterns

**`@task(timeout_seconds=3600, auto_heartbeat_seconds=30)`** — the `auto_heartbeat_seconds` parameter sends a Temporal heartbeat every 30 seconds automatically. Without heartbeats, Temporal marks a long-running task as timed out and retries it. You don't need to call `heartbeat()` manually when `auto_heartbeat_seconds` is set.

**Write to a temp file, then `upload_file`** — tasks run inside the Temporal worker process. Don't accumulate large lists in memory; write to a local temp file and upload it to object storage. The framework guarantees the upload path is accessible to downstream tasks.

**`resolve_credentials` inside the task, not in `run()`** — tasks may be retried on a different worker pod. Resolve credentials at the top of each task so every retry gets a fresh, valid credential.

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
            "connection": {
                "connection_name": "my-github",
                "connection_qualified_name": "default/github/1234567890",
            },
            "max_pages": 2,
        },
    )
)
```

Copy Dapr components into your project:

```bash
cp -r /path/to/application-sdk/components ./components
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
    set_infrastructure(
        InfrastructureContext(
            secret_store=MockSecretStore({
                "test-cred": {
                    "authType": "bearer",
                    "token": "fake-token",
                    "org": "test-org",
                }
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
            output_path=str(tmp_path),
            max_pages=5,
        )
    )

    assert result.record_count == 2

    lines = (tmp_path / "repos.jsonl").read_text().splitlines()
    entities = [json.loads(l) for l in lines]
    assert entities[0]["attributes"]["name"] == "repo-a"
    assert entities[1]["attributes"]["qualifiedName"] == "github/test-org/repo-b"
```

Run:

```bash
uv run pytest tests/ -v
```

### What the test demonstrates

- **`MockSecretStore`** intercepts `resolve_credentials()` without any Dapr sidecar. The dict you pass maps credential GUIDs to raw field dicts.
- **`set_infrastructure` / `clear_infrastructure`** swap the infrastructure context in and out per test. Always call `clear_infrastructure()` in teardown (the `yield` fixture pattern handles this).
- **`tmp_path`** (pytest built-in) gives each test an isolated temp directory. `upload_file` in tests writes to the local filesystem instead of object storage.
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
