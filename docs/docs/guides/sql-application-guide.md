# Building SQL Applications with Application SDK

The Application SDK provides a robust framework for building SQL applications that interact with databases to extract, process, and store metadata. This guide demonstrates how to leverage the SDK's default implementations while maintaining the flexibility to customize behavior for your specific needs.

## Overview

An SQL application built with the SDK typically performs the following operations:

1.  **Connect**: Securely connects to SQL databases using various authentication methods.
2.  **Extract**: Fetches metadata (schemas, tables, columns, procedures, etc.) using configurable SQL queries.
3.  **Validate**: Performs preflight checks and validates extracted metadata.
4.  **Transform**: Converts the extracted metadata into a standardized format (e.g., Atlas entities).
5.  **Store**: Saves the processed metadata to a configured object store.
6.  **Orchestrate**: Manages the entire process using Temporal workflows for reliability and scalability.

The SDK follows a "batteries included but swappable" philosophy - it provides sensible defaults for common tasks while allowing customization at every level.

## Core Components

The SDK's SQL metadata extraction workflow (`application_sdk.workflows.metadata_extraction.sql.BaseSQLMetadataExtractionWorkflow`) relies on three main customizable components defined in `application_sdk`:

1.  **SQL Client** (`application_sdk.clients.sql.BaseSQLClient`)
    *   **Responsibility**: Handles database connectivity and query execution.
    *   **Extensibility**: Extend this class to support different SQL dialects, connection string formats, or custom authentication logic (like IAM roles).

2.  **Activities** (`application_sdk.activities.metadata_extraction.sql.BaseSQLMetadataExtractionActivities`)
    *   **Responsibility**: Defines the SQL queries used to extract metadata (databases, schemas, tables, columns, etc.) and manages the extraction process.
    *   **Extensibility**: Override specific SQL query attributes (`fetch_database_sql`, `fetch_schema_sql`, etc.) to tailor metadata extraction for your database schema or specific needs.

3.  **Handler** (`application_sdk.handlers.sql.BaseSQLHandler`)
    *   **Responsibility**: Manages database-specific validation logic (e.g., checking table counts, validating filters) and provides helper methods for SQL operations.
    *   **Extensibility**: Extend this class to implement custom validation checks (`tables_check_sql`, `metadata_sql`) or database-specific logic required during the workflow.

These components work together within the `BaseSQLMetadataExtractionWorkflow`, orchestrated by a `application_sdk.worker.Worker`.

## Quick Start: Using Defaults

You can quickly get started by using the default implementations provided by the SDK. The main steps involve:

1.  Getting a workflow client.
2.  Instantiating the default activities (`BaseSQLMetadataExtractionActivities`) and optionally providing a custom SQL client class or handler class if needed (though defaults exist).
3.  Creating a `Worker` instance, passing the workflow client, the base workflow class (`BaseSQLMetadataExtractionWorkflow`), and the activities.
4.  Defining `workflow_args` with credentials and configuration.
5.  Starting the workflow and the worker.

*(See the full example in the "Putting It All Together" section below, which demonstrates both default usage and customization).*

## Customization Examples

While the defaults are powerful, you'll often need to customize components for specific databases or requirements. Here's how you can extend the core components, using examples inspired by `examples/application_sql.py`:

### 1. Custom SQL Client

Extend `BaseSQLClient` to define connection parameters specific to your database (e.g., PostgreSQL).

```python
# examples/application_sql.py
from application_sdk.clients.sql import BaseSQLClient

class SQLClient(BaseSQLClient):
    # Define connection string template and required parameters for PostgreSQL
    DB_CONFIG = {
        "template": "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        "required": ["username", "password", "host", "port", "database"],
    }
```

### 2. Custom Activities

Extend `BaseSQLMetadataExtractionActivities` to provide custom SQL queries for metadata extraction.

```python
# examples/application_sql.py
from application_sdk.activities.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
)

class SampleSQLActivities(BaseSQLMetadataExtractionActivities):
    # Customize database metadata query for PostgreSQL
    fetch_database_sql = """
    SELECT datname as database_name FROM pg_database WHERE datname = current_database();
    """

    # Customize schema metadata query for PostgreSQL
    fetch_schema_sql = """
    SELECT
        s.*
    FROM
        information_schema.schemata s
    WHERE
        s.schema_name NOT LIKE 'pg_%'
        AND s.schema_name != 'information_schema'
        AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) !~ '{normalized_exclude_regex}'
        AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) ~ '{normalized_include_regex}';
    """

    # Customize table metadata query
    fetch_table_sql = """
    SELECT
        t.*
    FROM
        information_schema.tables t
    WHERE concat(current_database(), concat('.', t.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', t.table_schema)) ~ '{normalized_include_regex}'
        {temp_table_regex_sql};
    """

    # Provide SQL snippet for excluding temporary tables (used in other queries)
    extract_temp_table_regex_table_sql = "AND t.table_name !~ '{exclude_table_regex}'"
    extract_temp_table_regex_column_sql = "AND c.table_name !~ '{exclude_table_regex}'"

    # Customize column metadata query
    fetch_column_sql = """
    SELECT
        c.*
    FROM
        information_schema.columns c
    WHERE
        concat(current_database(), concat('.', c.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', c.table_schema)) ~ '{normalized_include_regex}'
        {temp_table_regex_sql};
    """
```

### 3. Custom Handler

Extend `BaseSQLHandler` to implement custom validation logic specific to your database.

```python
# examples/application_sql.py
from application_sdk.handlers.sql import BaseSQLHandler

class SampleSQLWorkflowHandler(BaseSQLHandler):
    # Customize query to check table count (used in preflight checks)
    tables_check_sql = """
    SELECT count(*)
        FROM INFORMATION_SCHEMA.TABLES t -- Added alias 't'
        WHERE concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) !~ '{normalized_exclude_regex}'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) ~ '{normalized_include_regex}'
            AND TABLE_SCHEMA NOT IN ('performance_schema', 'information_schema', 'pg_catalog', 'pg_internal')
            {temp_table_regex_sql};
    """

    # Define SQL snippet for excluding temporary tables (used in tables_check_sql)
    temp_table_regex_sql = "AND t.table_name !~ '{exclude_table_regex}'" # Uses alias 't'

    # Customize query to fetch basic schema/catalog info (used in preflight checks)
    metadata_sql = """
    SELECT schema_name, catalog_name
        FROM INFORMATION_SCHEMA.SCHEMATA
        WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema'
    """
```

## Putting It All Together

This example, adapted from `examples/application_sql.py`, shows how to integrate custom components into a complete application:

```python
import asyncio
import os
from typing import Any, Dict

# Import SDK components
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.clients.utils import get_workflow_client
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.worker import Worker
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow
from application_sdk.observability.logger_adaptor import get_logger

APPLICATION_NAME = "postgres-app-example" # Define application name
logger = get_logger(__name__)

# --- Custom Component Definitions (as shown in previous sections) ---
class SQLClient(BaseSQLClient):
    DB_CONFIG = {
        "template": "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        "required": ["username", "password", "host", "port", "database"],
    }

class SampleSQLActivities(BaseSQLMetadataExtractionActivities):
    fetch_database_sql = "SELECT datname as database_name FROM pg_database WHERE datname = current_database();"
    fetch_schema_sql = "SELECT s.* FROM information_schema.schemata s WHERE s.schema_name NOT LIKE 'pg_%' AND s.schema_name != 'information_schema' AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) !~ '{normalized_exclude_regex}' AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) ~ '{normalized_include_regex}';"
    fetch_table_sql = "SELECT t.* FROM information_schema.tables t WHERE concat(current_database(), concat('.', t.table_schema)) !~ '{normalized_exclude_regex}' AND concat(current_database(), concat('.', t.table_schema)) ~ '{normalized_include_regex}' {temp_table_regex_sql};"
    extract_temp_table_regex_table_sql = "AND t.table_name !~ '{exclude_table_regex}'"
    extract_temp_table_regex_column_sql = "AND c.table_name !~ '{exclude_table_regex}'"
    fetch_column_sql = "SELECT c.* FROM information_schema.columns c WHERE concat(current_database(), concat('.', c.table_schema)) !~ '{normalized_exclude_regex}' AND concat(current_database(), concat('.', c.table_schema)) ~ '{normalized_include_regex}' {temp_table_regex_sql};"

class SampleSQLWorkflowHandler(BaseSQLHandler):
    tables_check_sql = "SELECT count(*) FROM INFORMATION_SCHEMA.TABLES t WHERE concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) !~ '{normalized_exclude_regex}' AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) ~ '{normalized_include_regex}' AND TABLE_SCHEMA NOT IN ('performance_schema', 'information_schema', 'pg_catalog', 'pg_internal') {temp_table_regex_sql};"
    temp_table_regex_sql = "AND t.table_name !~ '{exclude_table_regex}'"
    metadata_sql = "SELECT schema_name, catalog_name FROM INFORMATION_SCHEMA.SCHEMATA WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema'"
# --- End Custom Component Definitions ---

async def run_sql_application(daemon: bool = True) -> Dict[str, Any]:
    """Sets up and runs the SQL metadata extraction workflow."""
    logger.info(f"Starting SQL application: {APPLICATION_NAME}")

    # 1. Initialize workflow client (uses Temporal client configured via constants)
    workflow_client = get_workflow_client(application_name=APPLICATION_NAME)
    await workflow_client.load()

    # 2. Instantiate activities with custom SQL client and handler
    activities = SampleSQLActivities(
        sql_client_class=SQLClient, # Use our custom PostgreSQL client
        handler_class=SampleSQLWorkflowHandler # Use our custom handler
    )

    # 3. Set up the Temporal worker
    #    - Uses the base workflow class (handles orchestration)
    #    - Registers the activities instance (provides extraction logic)
    worker = Worker(
        workflow_client=workflow_client,
        workflow_classes=[BaseSQLMetadataExtractionWorkflow],
        workflow_activities=BaseSQLMetadataExtractionWorkflow.get_activities(activities),
    )

    # 4. Configure workflow arguments
    #    These arguments control credentials, connection details, filtering, etc.
    #    They are typically loaded from a secure source or environment variables.
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
            "connection_name": "test-postgres-connection", # Example connection name
            "connection_qualified_name": f"default/postgres/{int(time.time())}", # Example qualified name
        },
        "metadata": {
            # Define include/exclude filters (regex patterns)
            "exclude-filter": "{}",
            "include-filter": "{}",
            # Define regex for temporary tables to exclude
            "temp-table-regex": "^temp_",
            # Extraction method (direct connection)
            "extraction-method": "direct",
            # Options to exclude views or empty tables
            "exclude_views": "false",
            "exclude_empty_tables": "false",
        },
        "tenant_id": os.getenv("ATLAN_TENANT_ID", "default"), # Tenant ID from constants
        # --- Optional arguments ---
        # "workflow_id": "existing-workflow-run_id", # Uncomment to rerun a specific workflow
        # "cron_schedule": "0 */1 * * *", # Uncomment to run hourly
    }

    # 5. Start the workflow execution via the Temporal client
    logger.info(f"Starting workflow with args: {workflow_args}")
    workflow_response = await workflow_client.start_workflow(
        workflow_args, BaseSQLMetadataExtractionWorkflow
    )
    logger.info(f"Workflow started: {workflow_response}")

    # 6. Start the Temporal worker to process tasks
    #    Set daemon=True to run in background, False to run in foreground (for scripts)
    logger.info(f"Starting worker (daemon={daemon})...")
    await worker.start(daemon=daemon)

    return workflow_response # Returns info like workflow_id, run_id

# --- Script Execution ---
if __name__ == "__main__":
    import time # Needed for connection_qualified_name example
    # Run the application in the foreground when script is executed directly
    asyncio.run(run_sql_application(daemon=False))

```

## Configuration Details

The `workflow_args` dictionary is crucial for controlling the workflow's behavior. Key sections include:

*   **`credentials`**: Contains database connection details (`host`, `port`, `username`, `password`, `database`) and the `authType` (e.g., `basic`, `iam_user`, `iam_role`). Sensitive values should be loaded securely (e.g., from environment variables or a secret store).
*   **`connection`**: Defines how the connection is identified in Atlan (`connection_name`, `connection_qualified_name`).
*   **`metadata`**: Controls extraction behavior:
    *   `include-filter`/`exclude-filter`: JSON strings containing regex patterns to filter schemas/tables.
    *   `temp-table-regex`: Regex to identify temporary tables for exclusion.
    *   `extraction-method`: Typically "direct".
    *   `exclude_views`: Boolean string ("true"/"false") to skip views.
    *   `exclude_empty_tables`: Boolean string to skip tables with zero rows (requires count checks).
*   **`tenant_id`**: Specifies the tenant context.
*   **Optional**: `workflow_id` for reruns, `cron_schedule` for scheduled execution.

Refer to the [Configuration Guide](./configuration.md) for details on setting these via environment variables.

## Advanced Features

### Scheduled Execution

Run workflows automatically by adding `cron_schedule` to `workflow_args`:

```python
workflow_args["cron_schedule"] = "0 1 * * *" # Run daily at 1 AM
```

### Workflow Rerun

Resume or rerun a specific workflow execution by providing its `workflow_id`:

```python
workflow_args["workflow_id"] = "your-previous-workflow_id"
```

### Custom Authentication

The `BaseSQLClient` supports different `authType` values in the `credentials`:

*   `basic`: Uses `username` and `password`.
*   `iam_user`: Uses AWS IAM user credentials (requires specific keys in `credentials`).
*   `iam_role`: Uses an AWS IAM role (requires specific keys in `credentials`).

You can extend `BaseSQLClient` to add support for other authentication mechanisms.

## Best Practices

1.  **Configuration**: Load sensitive credentials and configuration from environment variables or secure stores, not directly in code. Use the constants defined in `application_sdk.constants`.
2.  **Error Handling**: Implement `try...except` blocks in custom code (especially within Activities or Handlers) to handle potential database errors or unexpected data.
3.  **Logging**: Use the SDK's logger (`application_sdk.observability.logger_adaptor.get_logger`) for consistent and structured logging integrated with Temporal.
4.  **Idempotency**: Design activities to be idempotent where possible, meaning they can be run multiple times with the same result. Temporal handles retries, but idempotent activities simplify recovery.
5.  **Testing**: Write unit tests for your custom Client, Activities, and Handler classes to ensure they function correctly. The SDK provides testing utilities (`application_sdk.test_utils`).
6.  **Resource Management**: Ensure database connections are properly closed. The SDK's client and workflow management generally handle this, but be mindful in custom extensions.

## Next Steps

*   Explore the complete [PostgreSQL example application](https://github.com/atlanhq/atlan-postgres-app) for a more production-ready implementation.
*   Consult the [Configuration Guide](./configuration.md) for managing environment variables.