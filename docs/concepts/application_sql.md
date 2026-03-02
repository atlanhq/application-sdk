# SQL Application

This module provides a high-level abstraction for building SQL metadata extraction applications using the Atlan Application SDK. It standardizes the setup and orchestration of SQL workflows, activities, handlers, and transformers, leveraging Temporal for workflow execution and FastAPI for serving APIs.

## Core Concepts

1.  **`BaseSQLMetadataExtractionApplication` (`application_sdk.application.metadata_extraction.sql.py`)**:
    *   **Purpose:** Provides a reusable, extensible base class for SQL metadata extraction applications. Handles workflow client setup, worker registration, and FastAPI server integration.
    *   **Extensibility:** Accepts custom SQL client, handler, and transformer classes, allowing adaptation for different SQL dialects and metadata models.
    *   **Key Methods:**
        - `setup_workflow()`: Registers and starts the Temporal worker for SQL workflows.
        - `start_workflow()`: Initiates a new workflow execution with provided arguments.
        - `setup_server()`: Sets up the FastAPI server and registers workflow endpoints.
        - `start_server()`: Starts the FastAPI server.

2.  **Custom SQL Client, Handler, and Transformer**:
    *   **SQL Client:** Subclass `BaseSQLClient` to implement connection logic for your target database (e.g., PostgreSQL, Snowflake).
    *   **Handler:** Subclass `BaseSQLHandler` to customize authentication, metadata fetching, and preflight checks.
    *   **Transformer:** Subclass `AtlasTransformer` to map extracted metadata to custom entity models if needed.

3.  **Activities and Workflows**:
    *   **Activities:** Use or extend `BaseSQLMetadataExtractionActivities` to define the steps for metadata extraction (e.g., fetch databases, schemas, tables, columns).
    *   **Workflow:** Use `BaseSQLMetadataExtractionWorkflow` or subclass it for custom orchestration logic.

## Usage Patterns

### 1. Basic SQL Metadata Extraction Application

```python
from application_sdk.application.metadata_extraction.sql import BaseSQLMetadataExtractionApplication
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow

class SQLClient(BaseSQLClient):
    DB_CONFIG = {
        "template": "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        "required": ["username", "password", "host", "port", "database"],
    }

class SampleSQLHandler(BaseSQLHandler):
    # Optionally override SQL queries or methods
    pass

class SampleSQLActivities(BaseSQLMetadataExtractionActivities):
    # Optionally override SQL query strings
    pass

app = BaseSQLMetadataExtractionApplication(
    name="postgres",
    sql_client_class=SQLClient,
    handler_class=SampleSQLHandler,
)

await app.setup_workflow(
    workflow_and_activities_classes=[
        (BaseSQLMetadataExtractionWorkflow, SampleSQLActivities)
    ],
    worker_daemon_mode=True,
)

workflow_args = {
    "credentials": { ... },
    "connection": { ... },
    "metadata": { ... },
    "tenant_id": "123",
}

workflow_response = await app.start_workflow(workflow_args=workflow_args)
```

### 2. Customizing Activities and Transformers

You can override SQL queries or add custom transformation logic by subclassing the base activities or transformer:

```python
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities
from application_sdk.transformers.atlas import AtlasTransformer

class CustomSQLActivities(BaseSQLMetadataExtractionActivities):
    fetch_database_sql = """
    SELECT datname as database_name FROM pg_database WHERE datname = current_database();
    """
    # ... other custom queries ...

class CustomTransformer(AtlasTransformer):
    def __init__(self, connector_name, tenant_id, **kwargs):
        super().__init__(connector_name, tenant_id, **kwargs)
        # Custom entity mapping
        self.entity_class_definitions["DATABASE"] = MyCustomDatabaseEntity
```

### 3. Running the FastAPI Server

```python
await app.setup_server()
await app.start_server(daemon=True)
```

## Example: PostgreSQL Metadata Extraction

The following example demonstrates a complete SQL metadata extraction application for PostgreSQL, using the abstraction:

```python
import asyncio
import os
from application_sdk.application.metadata_extraction.sql import BaseSQLMetadataExtractionApplication
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow

class SQLClient(BaseSQLClient):
    DB_CONFIG = {
        "template": "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        "required": ["username", "password", "host", "port", "database"],
    }

class SampleSQLHandler(BaseSQLHandler):
    pass

class SampleSQLActivities(BaseSQLMetadataExtractionActivities):
    fetch_database_sql = """
    SELECT datname as database_name FROM pg_database WHERE datname = current_database();
    """
    # ... other custom queries ...

async def main():
    app = BaseSQLMetadataExtractionApplication(
        name="postgres",
        sql_client_class=SQLClient,
        handler_class=SampleSQLHandler,
    )
    await app.setup_workflow(
        workflow_and_activities_classes=[
            (BaseSQLMetadataExtractionWorkflow, SampleSQLActivities)
        ],
        worker_daemon_mode=True,
    )
    workflow_args = {
        "credentials": {
            "authType": "basic",
            "host": os.getenv("POSTGRES_HOST", "localhost"),
            "port": os.getenv("POSTGRES_PORT", "5432"),
            "username": os.getenv("POSTGRES_USER", "postgres"),
            "password": os.getenv("POSTGRES_PASSWORD", "password"),
            "database": os.getenv("POSTGRES_DATABASE", "postgres"),
        },
        "connection": {
            "connection_name": "test-connection",
            "connection_qualified_name": "default/postgres/1728518400",
        },
        "metadata": {
            "exclude-filter": "{}",
            "include-filter": "{}",
            "temp-table-regex": "",
            "extraction-method": "direct",
            "exclude_views": "true",
            "exclude_empty_tables": "false",
        },
        "tenant_id": "123",
    }
    workflow_response = await app.start_workflow(workflow_args=workflow_args)
    print(workflow_response)

if __name__ == "__main__":
    asyncio.run(main())
```

## Extension Points

- **Custom SQL Dialects:** Subclass `BaseSQLClient` and override `DB_CONFIG` for your target database.
- **Custom Metadata Logic:** Subclass `BaseSQLHandler` to override authentication, metadata fetching, or preflight checks.
- **Custom Activities:** Subclass `BaseSQLMetadataExtractionActivities` to override or add activity methods or SQL queries.
- **Custom Transformers:** Subclass `AtlasTransformer` to map extracted metadata to custom entity models.

## Summary

The `BaseSQLMetadataExtractionApplication` abstraction enables rapid development of robust, extensible SQL metadata extraction applications. By providing clear extension points for clients, handlers, activities, and transformers, it supports a wide range of SQL sources and metadata models, while standardizing workflow orchestration and API serving.