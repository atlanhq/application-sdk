---
name: scaffold-app
description: >
  Bootstrap a new Atlan app repo from scratch. Generates pyproject.toml,
  Dockerfile, atlan.yaml, app/ package skeleton (contracts, handler, connector,
  clients), .env.example, and an initial run_dev.py. Branches on connector
  type (SQL vs REST) and auth type (basic, api_key, bearer, oauth).
mandatory_triggers:
  - "/scaffold-app"
  - "scaffold a new app"
  - "create a new connector"
optional_triggers:
  - "bootstrap app"
  - "new connector"
  - "create app skeleton"
owner: connector-platform-team
last_updated: "2026-04-30"
staleness_days: 90
inputs:
  - app_name: string (kebab-case, e.g. "my-connector")
  - connector_type: enum[sql, rest] (default: sql)
  - auth_type: enum[basic, api_key, bearer, oauth_client] (default: basic)
outputs:
  - pyproject.toml
  - atlan.yaml
  - Dockerfile
  - .env.example
  - app/__init__.py
  - app/contracts.py
  - app/handler.py
  - app/connector.py
  - app/clients.py (sql only)
  - run_dev.py
gates: []
---

# scaffold-app

Bootstrap a new Atlan Application SDK connector repo from scratch. Run this first
when creating a brand-new connector — before running the `contract` skill.

## What Gets Generated

```
{app_name}/
├── pyproject.toml             # uv project with atlan-application-sdk dep
├── atlan.yaml                 # App manifest (app_id, execution_mode, dapr config)
├── Dockerfile                 # SDK base image + uv sync + ENV ATLAN_APP_MODULE
├── .env.example               # All required env vars with example values
├── run_dev.py                 # Local dev script using run_dev_combined()
└── app/
    ├── __init__.py
    ├── contracts.py           # Input/Output models + credential model
    ├── handler.py             # Handler subclass (test_auth, preflight, metadata)
    ├── connector.py           # App subclass with @task methods
    └── clients.py             # BaseSQLClient subclass (SQL connectors only)
```

## Steps

### 1. Gather inputs

Ask the user for:
- `app_name` (kebab-case): e.g. `my-postgres-connector`
- `connector_type`: `sql` or `rest`
- `auth_type`: `basic`, `api_key`, `bearer`, or `oauth_client`

Infer class names: `MyPostgresConnector`, `MyPostgresHandler`, `MyPostgresClient` from kebab-case.

### 2. Generate `pyproject.toml`

```toml
[project]
name = "{app_name}"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = [
    "atlan-application-sdk>=3.0.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
```

### 3. Generate `atlan.yaml`

```yaml
app_id: {app_name}
execution_mode: native
splitDeploymentEnabled: true
dapr:
  objectstore:
    enabled: true
  secretstore:
    enabled: true
```

### 4. Generate `Dockerfile`

```dockerfile
FROM ghcr.io/atlanhq/application-sdk:3.4.0

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --frozen

COPY app/ app/

ENV ATLAN_APP_MODULE=app.connector:{ClassName}App
ENV ATLAN_CONTRACT_GENERATED_DIR=app/generated
```

### 5. Generate `app/contracts.py`

Imports from `application_sdk.contracts`. Include:
- `{Name}Input(Input)` with `connection_id: str` and `credential_guid: str`
- `{Name}Output(Output)` with `record_count: int`
- Credential model matching the selected `auth_type`

### 6. Generate `app/handler.py`

Subclass `Handler` with `test_auth`, `preflight_check`, `fetch_metadata`. Use the appropriate
credential type from `contracts.py`. Return typed `AuthOutput`, `PreflightOutput`, `MetadataOutput`.

For SQL connectors, `fetch_metadata` should use the SQL client to list databases/schemas.

### 7. Generate `app/connector.py` (SQL variant)

```python
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput, ExtractionOutput,
    FetchDatabasesInput, FetchDatabasesOutput,
)
from application_sdk.app import task

class {Name}App(SqlMetadataExtractor):
    sql_client_class = {Name}Client

    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        client = await self._load_sql_client(input)
        async for batch in client.run_query(self.fetch_database_sql):
            # process batch
            pass
        return FetchDatabasesOutput(chunk_count=1, total_record_count=10)
```

For REST connectors, subclass `App` directly with custom `@task` methods.

### 8. Generate `app/clients.py` (SQL only)

```python
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.clients.models import DatabaseConfig

class {Name}Client(BaseSQLClient):
    def _make_connection_string(self, config: DatabaseConfig) -> str:
        return f"postgresql+asyncpg://{config.username}:{config.password}@{config.host}:{config.port}/{config.database}"
```

### 9. Generate `.env.example`

Include all required env vars with placeholder values:
```bash
ATLAN_APP_MODULE=app.connector:{Name}App
ATLAN_TEMPORAL_HOST=localhost:7233
DAPR_HTTP_PORT=3500
DAPR_GRPC_PORT=50001
ATLAN_LOG_LEVEL=DEBUG
```

### 10. Generate `run_dev.py`

```python
import asyncio
from application_sdk.main import run_dev_combined
from app.connector import {Name}App

asyncio.run(
    run_dev_combined(
        {Name}App,
        credentials={
            "host": "localhost",
            "port": "5432",
            "authType": "basic",
            "username": "admin",
            "password": "secret",
            "extra": {"database": "mydb"},
        },
        example_input={
            "connection": {
                "connection_name": "test-connection",
                "connection_qualified_name": "default/{app_name}/1234567890",
            },
        },
    )
)
```

## After Scaffolding

Tell the user:
1. Run `uv sync` to install dependencies.
2. Copy and configure `.env.example → .env`.
3. Start local deps: `temporal server start-dev` + `dapr run ...` (see [Getting Started](../../docs/guides/getting-started.md)).
4. Run `uv run python run_dev.py` to test the scaffold.
5. Run the `contract` skill to generate the PKL contract and `app/generated/` artifacts.
