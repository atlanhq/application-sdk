# Building a REST API Connector

This guide builds a complete REST API connector from scratch — an app that calls an HTTP API, transforms the response into Atlan metadata, and uploads it. Use this when you're connecting to a SaaS tool, file-based source, or any non-SQL data source.

For SQL connectors, see [Creating an SQL Application](sql-application-guide.md).

## What You're Building

A connector that:

1. Authenticates with an API using an API key
2. Fetches entities (e.g. tables, pipelines, dashboards)
3. Writes JSONL output to object storage
4. Uploads the output to Atlan via `upload_to_atlan`

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

from dataclasses import dataclass, field

from application_sdk.contracts.base import Input, Output
from application_sdk.credentials.types import ApiKeyCredential


# --- Credential model ---

class MyApiCredential(ApiKeyCredential):
    """Credential for My API. api_token maps to the API key."""
    base_url: str = "https://api.example.com"


# --- Workflow contracts ---

@dataclass
class ExtractionInput(Input):
    output_path: str = ""
    days_back: int = 30


@dataclass
class ExtractionOutput(Output):
    output_path: str = ""
    record_count: int = 0


@dataclass
class FetchEntitiesInput(Input):
    credential_guid: str = ""
    output_path: str = ""
    days_back: int = 30


@dataclass
class FetchEntitiesOutput(Output):
    output_path: str = ""
    record_count: int = 0
```

## HTTP Client

Put your HTTP client in `app/clients.py`:

```python
from __future__ import annotations

import httpx

from app.contracts import MyApiCredential


class MyApiClient:
    def __init__(self, credential: MyApiCredential) -> None:
        self._client = httpx.AsyncClient(
            base_url=credential.base_url,
            headers={"Authorization": f"Bearer {credential.api_token}"},
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
    AuthInput, AuthOutput,
    PreflightInput, PreflightOutput,
)
from application_sdk.credentials import resolve_credentials

from app.clients import MyApiClient
from app.contracts import MyApiCredential


class MyConnectorHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        credential = await resolve_credentials(
            input.credential_guid, MyApiCredential
        )
        client = MyApiClient(credential)
        try:
            entities = await client.get_entities(page=1)
            return AuthOutput(success=True, count=len(entities))
        finally:
            await client.close()

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(success=True)
```

## App (Connector)

The App subclasses `BaseMetadataExtractor`, which provides the `upload_to_atlan` task:

```python
from __future__ import annotations

import json
import os
import tempfile

from application_sdk.app.task import task
from application_sdk.credentials import resolve_credentials
from application_sdk.storage import upload_file
from application_sdk.templates import BaseMetadataExtractor
from application_sdk.templates.contracts.base_metadata_extraction import (
    UploadInput,
)

from app.clients import MyApiClient
from app.contracts import (
    ExtractionInput,
    ExtractionOutput,
    FetchEntitiesInput,
    FetchEntitiesOutput,
    MyApiCredential,
)


class MyConnectorApp(BaseMetadataExtractor):
    handler_class = None  # set in atlan.yaml or via --handler flag

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        fetch_out = await self.fetch_entities(
            FetchEntitiesInput(
                credential_guid=input.credential_guid,
                output_path=input.output_path,
                days_back=input.days_back,
            )
        )
        await self.upload_to_atlan(
            UploadInput(output_path=fetch_out.output_path)
        )
        return ExtractionOutput(
            output_path=fetch_out.output_path,
            record_count=fetch_out.record_count,
        )

    @task(timeout_seconds=3600, auto_heartbeat_seconds=30)
    async def fetch_entities(
        self, input: FetchEntitiesInput
    ) -> FetchEntitiesOutput:
        credential = await resolve_credentials(
            input.credential_guid, MyApiCredential
        )
        client = MyApiClient(credential)

        output_file = os.path.join(input.output_path, "entities.jsonl")
        record_count = 0

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False
            ) as tmp:
                page = 1
                while True:
                    entities = await client.get_entities(page=page)
                    if not entities:
                        break
                    for entity in entities:
                        # Transform to Atlan entity format
                        atlan_entity = _transform_entity(entity)
                        tmp.write(json.dumps(atlan_entity) + "\n")
                        record_count += 1
                    page += 1

                tmp_path = tmp.name

            await upload_file(output_file, tmp_path)
        finally:
            await client.close()
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        return FetchEntitiesOutput(
            output_path=input.output_path,
            record_count=record_count,
        )


def _transform_entity(raw: dict) -> dict:
    """Transform a raw API entity to Atlan entity format."""
    return {
        "typeName": "Table",
        "attributes": {
            "name": raw["name"],
            "qualifiedName": raw["id"],
            "description": raw.get("description", ""),
        },
    }
```

### Why `BaseMetadataExtractor`?

`BaseMetadataExtractor` is an `App` subclass that adds a single built-in task: `upload_to_atlan`. This task copies files from your deployment object store to Atlan's upstream store, which is what triggers ingestion. You don't need to inherit it if you handle file transfer yourself, but it saves boilerplate for the typical REST connector.

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
    "credential": {
      "authType": "api_key",
      "api_token": "my-test-api-token",
      "base_url": "https://api.example.com"
    },
    "connection": {
      "qualifiedName": "default/my-connector/1234567890",
      "name": "my-test-connection"
    }
  }'
```

## Payload Safety

If any of your task inputs or outputs could be large, use `FileReference` instead of embedding data directly:

```python
from application_sdk.contracts.base import Input
from application_sdk.contracts.storage import FileReference, StorageTier

@dataclass
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

For tasks where you want manual control (e.g. to include progress in the heartbeat):

```python
from temporalio.activity import heartbeat

@task(timeout_seconds=7200)
async def fetch_entities(self, input: FetchEntitiesInput) -> FetchEntitiesOutput:
    for page in range(total_pages):
        # process page...
        heartbeat({"page": page, "total": total_pages})
```

## Error Handling

Raise `ActivityError` for failures that should mark the task as failed in Temporal:

```python
from application_sdk.common.error_codes import ActivityError

@task(timeout_seconds=3600)
async def fetch_entities(self, input: FetchEntitiesInput) -> FetchEntitiesOutput:
    try:
        ...
    except httpx.HTTPStatusError as e:
        raise ActivityError(
            f"API request failed: {e.response.status_code}"
        ) from e
```

## Testing

Use `MockSecretStore` and `MockCredentialStore` to test without real credentials:

```python
import pytest
from application_sdk.infrastructure import set_infrastructure, clear_infrastructure
from application_sdk.infrastructure.context import InfrastructureContext
from application_sdk.testing import MockSecretStore

@pytest.fixture(autouse=True)
def infra(tmp_path):
    set_infrastructure(
        InfrastructureContext(
            secret_store=MockSecretStore({
                "my-test-cred": {
                    "authType": "api_key",
                    "api_token": "test-token",
                    "base_url": "http://localhost",
                }
            })
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
