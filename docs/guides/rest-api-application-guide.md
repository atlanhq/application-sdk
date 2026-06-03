# Building a REST API Connector

This guide builds a complete REST API connector from scratch — an app that calls an HTTP API, transforms the response into Atlan metadata, and uploads it. Use this when you're connecting to a SaaS tool, file-based source, or any non-SQL data source.

For SQL connectors, see [Creating an SQL Application](sql-application-guide.md).

## What You're Building

A connector that:

1. Authenticates with an API using an API key
2. Fetches entities (e.g. tables, pipelines, dashboards)
3. Writes JSONL output to object storage
4. Uploads the output to Atlan via `App.upload()`

## Project Setup

```bash
uv init my-connector && cd my-connector
uv add "atlan-application-sdk"
uv add httpx  # or aiohttp, requests — your HTTP client of choice
mkdir -p app
```

## Contracts

Define input, output, and credential types in `app/contracts.py`:

```python
from __future__ import annotations

from application_sdk.contracts import Input, Output


# --- Workflow contracts ---

class ExtractionInput(Input):
    credential_guid: str = ""
    output_path: str = ""
    days_back: int = 30


class ExtractionOutput(Output):
    output_path: str = ""
    record_count: int = 0


class FetchEntitiesInput(Input):
    credential_guid: str = ""
    output_path: str = ""
    days_back: int = 30


class FetchEntitiesOutput(Output):
    output_path: str = ""
    record_count: int = 0
```

`Input` and `Output` are Pydantic `BaseModel` subclasses — do not use `@dataclass` on them.

## HTTP Client

Put your HTTP client in `app/clients.py`:

```python
from __future__ import annotations

import httpx

class MyApiClient:
    def __init__(self, api_token: str, base_url: str = "https://api.example.com") -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30,
        )

    async def get_entities(self, page: int = 1) -> list[dict]:
        response = await self._client.get("/v1/entities", params={"page": page})
        response.raise_for_status()
        return response.json()["data"]

    async def close(self) -> None:
        await self._client.aclose()
```

## Handler

The handler wires up `test_auth` and `preflight_check`:

```python
from __future__ import annotations

from application_sdk.handler import Handler
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    MetadataInput, ApiMetadataOutput,
    PreflightInput, PreflightOutput, PreflightStatus,
)

from app.clients import MyApiClient


class MyConnectorHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        # Credentials arrive as a list of key/value pairs.
        api_token = next((c.value for c in input.credentials if c.key == "api_token"), "")
        base_url = next(
            (c.value for c in input.credentials if c.key == "base_url"),
            "https://api.example.com",
        )
        client = MyApiClient(api_token=api_token, base_url=base_url)
        try:
            entities = await client.get_entities(page=1)
            return AuthOutput(
                status=AuthStatus.SUCCESS,
                message=f"Connected: {len(entities)} entities found",
            )
        finally:
            await client.close()

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="OK")

    async def fetch_metadata(self, input: MetadataInput) -> ApiMetadataOutput:
        # Metadata extraction is driven by the App, not the handler.
        return ApiMetadataOutput()
```

## App (Connector)

The App subclasses `App` directly (REST connectors typically manage their own output pipeline):

```python
# app/connector.py
from __future__ import annotations

import json
import os

from application_sdk.app import App, task
from application_sdk.contracts import UploadInput, StorageTier
from application_sdk.credentials import CredentialResolver, api_key_ref
from application_sdk.infrastructure import get_infrastructure

from app.clients import MyApiClient
from app.contracts import (
    ExtractionInput,
    ExtractionOutput,
    FetchEntitiesInput,
    FetchEntitiesOutput,
)


class MyConnectorApp(App):

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        fetch_out = await self.fetch_entities(
            FetchEntitiesInput(
                credential_guid=input.credential_guid,
                output_path=input.output_path,
                days_back=input.days_back,
            )
        )
        # Explicit hand-off to Atlan: routes through atlan-objectstore (Atlan-owned)
        # in SDR deployments so the publish app can read it. The activity interceptor
        # only writes FileReferences to the customer-owned objectstore (infra.storage).
        # Omitting this call produces a silent failure in SDR — the DAG succeeds but
        # the publish app finds nothing. See ADR-0014.
        await self.upload(UploadInput(local_path=fetch_out.output_path))
        return ExtractionOutput(
            output_path=fetch_out.output_path,
            record_count=fetch_out.record_count,
        )

    @task(timeout_seconds=3600, auto_heartbeat_seconds=30)
    async def fetch_entities(
        self, input: FetchEntitiesInput
    ) -> FetchEntitiesOutput:
        infra = get_infrastructure()
        resolver = CredentialResolver(secret_store=infra.secret_store)
        ref = api_key_ref(input.credential_guid)
        raw = await resolver.resolve_raw(ref)
        client = MyApiClient(
            api_token=raw.get("api_token", ""),
            base_url=raw.get("base_url", "https://api.example.com"),
        )

        os.makedirs(input.output_path, exist_ok=True)
        output_file = os.path.join(input.output_path, "entities.jsonl")
        record_count = 0

        try:
            with open(output_file, "w") as f:
                page = 1
                while True:
                    entities = await client.get_entities(page=page)
                    if not entities:
                        break
                    for entity in entities:
                        atlan_entity = _transform_entity(entity)
                        f.write(json.dumps(atlan_entity) + "\n")
                        record_count += 1
                page += 1
        finally:
            await client.close()

        return FetchEntitiesOutput(
            output_path=input.output_path,
            record_count=record_count,
        )


def _transform_entity(raw: dict) -> dict:
    return {
        "typeName": "Table",
        "attributes": {
            "name": raw["name"],
            "qualifiedName": raw["id"],
            "description": raw.get("description", ""),
        },
    }
```

> **Silent-failure rule:** `App.upload()` in `run()` is required for any data that Atlan system apps
> (publish, QI, lineage) must consume. The activity interceptor only writes `FileReference` objects
> to the **customer-owned** `objectstore` (`infra.storage`). In SDR deployments, `App.upload()`
> routes through `atlan-objectstore` (`infra.upstream_storage`, Atlan-owned). Connectors that omit
> this call produce a silent failure — the DAG completes normally but the publish app finds nothing
> in Atlan's bucket. See [ADR-0014](../adr/0014-two-store-storage-architecture.md).

## Local Development

Create `run_dev.py`:

```python
import asyncio
from application_sdk.main import run_dev_combined
from app.connector import MyConnectorApp

asyncio.run(
    run_dev_combined(
        MyConnectorApp,
        credentials={
            "authType": "api_key",
            "api_token": "my-test-api-token",
            "base_url": "https://api.example.com",
        },
        example_input={
            "connection": {
                "connection_name": "test-connection",
                "connection_qualified_name": "default/my-connector/1234567890",
            },
            "days_back": 7,
        },
    )
)
```

Start dependencies and run:

```bash
# Terminal 1 — Temporal
temporal server start-dev --db-filename temporal.db

# Terminal 2 — Dapr
dapr run \
  --app-id app \
  --app-port 8000 \
  --dapr-http-port 3500 \
  --dapr-grpc-port 50001 \
  --scheduler-host-address '' \
  --placement-host-address '' \
  --config components/configuration.yaml \
  --resources-path components &

# Terminal 3 — App
DAPR_HTTP_PORT=3500 DAPR_GRPC_PORT=50001 uv run python run_dev.py
```

Trigger the workflow:

```bash
curl -X POST http://localhost:8000/workflows/v1/start \
  -H "Content-Type: application/json" \
  -d '{
    "credential_guid": "test-local-cred",
    "connection": {
      "qualifiedName": "default/my-connector/1234567890",
      "name": "my-test-connection"
    }
  }'
```

## Payload Safety

If any of your task inputs or outputs could be large, use `FileReference` instead of embedding data directly:

```python
from application_sdk.contracts import Input, FileReference, StorageTier

class FetchEntitiesInput(Input):
    credential_guid: str = ""
    output_path: str = ""
    # Don't pass entity lists as task args — use FileReference instead
```

See [Storage](../concepts/storage.md) for the full pattern.

## Heartbeats in Long Tasks

For tasks that run more than a few minutes, use `auto_heartbeat_seconds` to let Temporal know the task is alive:

```python
@task(timeout_seconds=7200, auto_heartbeat_seconds=30)
async def fetch_entities(self, input: FetchEntitiesInput) -> FetchEntitiesOutput:
    ...
```

For tasks where you want manual control (e.g. to include progress in the heartbeat), define a typed `HeartbeatDetails` subclass so that `task_context.get_heartbeat_details(cls)` can recover state on retry:

```python
from application_sdk.contracts import HeartbeatDetails

class PageProgress(HeartbeatDetails):
    page: int = 0
    total: int = 0

@task(timeout_seconds=7200)
async def fetch_entities(self, input: FetchEntitiesInput) -> FetchEntitiesOutput:
    for page in range(total_pages):
        # process page...
        self.task_context.heartbeat(PageProgress(page=page, total=total_pages))
```

Never import from `temporalio` directly — use `self.task_context.heartbeat()` which wraps the Temporal primitive.

## Error Handling

Raise `NonRetryableError` for failures that should permanently fail the task in Temporal (no retries):

```python
from application_sdk.app import NonRetryableError

@task(timeout_seconds=3600)
async def fetch_entities(self, input: FetchEntitiesInput) -> FetchEntitiesOutput:
    try:
        ...
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 401, 403, 404):
            raise NonRetryableError(
                f"API request failed permanently: {e.response.status_code}"
            ) from e
        raise  # let Temporal retry transient errors (5xx, network)
```

## Testing

Use `MockSecretStore` to test without real credentials:

```python
import json
import pytest
from application_sdk.infrastructure import set_infrastructure, clear_infrastructure, InfrastructureContext
from application_sdk.storage import create_memory_store
from application_sdk.testing import MockSecretStore

@pytest.fixture(autouse=True)
def infra(tmp_path):
    # MockSecretStore stores secrets as JSON strings (same as the real Dapr secret store).
    # create_memory_store() provides an in-process object store so upload_file() works without Dapr.
    set_infrastructure(
        InfrastructureContext(
            secret_store=MockSecretStore({
                "my-test-cred": json.dumps({
                    "type": "api_key",
                    "api_token": "test-token",
                    "base_url": "http://localhost",
                })
            }),
            storage=create_memory_store(),
        )
    )
    yield
    clear_infrastructure()


@pytest.mark.asyncio
async def test_fetch_entities(httpx_mock, tmp_path):
    httpx_mock.add_response(
        url="http://localhost/v1/entities?page=1",
        json={"data": [{"id": "e1", "name": "Entity 1"}]},
    )
    httpx_mock.add_response(
        url="http://localhost/v1/entities?page=2",
        json={"data": []},
    )

    app = MyConnectorApp()
    result = await app.fetch_entities(
        FetchEntitiesInput(
            credential_guid="my-test-cred",
            output_path=str(tmp_path),
        )
    )
    assert result.record_count == 1
```

## See Also

- [Getting Started](getting-started.md) — first-run walkthrough
- [Creating an SQL Application](sql-application-guide.md) — SQL connector guide
- [Contracts](../concepts/contracts.md) — Input/Output contracts and payload safety
- [Credentials](../concepts/credentials.md) — typed credential system
- [Storage](../concepts/storage.md) — FileReference and StorageTier
- [Tasks](../concepts/tasks.md) — task configuration, heartbeats, retries
