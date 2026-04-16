# Clients

This module provides the necessary abstractions (clients) for interacting with various external systems required by the application workflows, such as databases and workflow orchestration engines (Temporal).

## Core Concepts

1.  **`ClientInterface` (`application_sdk.clients.__init__.py`)**:
    *   **Purpose:** An abstract base class defining the minimal contract for all clients. It requires implementing an `async def load()` method for connection/setup and provides an optional `async def close()` for cleanup.
    *   **Extensibility:** Any class interacting with an external service should ideally inherit from this interface.

2.  **Specialized Clients:** The SDK provides concrete client implementations for specific services:
    *   **SQL Databases (`sql.py`):** For connecting to and querying SQL databases.
    *   **Non-SQL Systems (`base.py`):** For connecting to non-SQL data sources like REST APIs, or other services.
    *   **Temporal (`temporal.py`, `workflow.py`):** For connecting to the Temporal service and managing workflow executions.

## SQL Client (`sql.py`)

Provides classes for interacting with SQL databases using SQLAlchemy.

### Key Classes

*   **`BaseSQLClient(ClientInterface)`**:
    *   **Purpose:** Handles synchronous connections and query execution using SQLAlchemy's standard engine and connection pool. Good for activities or setup steps that don't require high concurrency within the client itself.
    *   **Query Execution:** Uses `ThreadPoolExecutor` internally for `run_query` to avoid blocking the asyncio event loop during potentially long-running synchronous database operations.
*   **`AsyncBaseSQLClient(BaseSQLClient)`**:
    *   **Purpose:** Handles asynchronous connections and query execution using SQLAlchemy's async features (`create_async_engine`, `AsyncConnection`). Suitable for scenarios requiring non-blocking database I/O.
    *   **Query Execution:** Uses `async/await` directly with the async SQLAlchemy connection for `run_query`.

### Configuration and Usage

Both SQL client classes are typically **subclassed** for specific database types (e.g., PostgreSQL, Snowflake) rather than used directly.

1.  **Connection Configuration (`DB_CONFIG` - Class Attribute):**
    *   Define `DB_CONFIG` using the Pydantic model `DatabaseConfig` (`application_sdk.clients.models.DatabaseConfig`).
    *   **`template` (str):** SQLAlchemy connection string template using placeholders (e.g., `{username}`, `{host}`).
    *   **`required` (list[str]):** Keys that must be present in `credentials`/`credentials.extra`. `{password}` is resolved via `get_auth_token()` depending on `authType`.
    *   **`parameters` (list[str], optional):** Optional keys appended as URL query parameters when present in `credentials`/`extra`.
    *   **`defaults` (dict[str, Any], optional):** Default URL parameters always appended unless already in the template.
    *   **`connect_args` (dict[str, Any], optional):** Additional connection arguments to be passed directly to SQLAlchemy's `create_engine` or `create_async_engine`. Useful for driver-specific connection parameters that are not part of the connection URL. Defaults to `{}`.
    *   **Credentials Note:** The `credentials` dictionary can include an `extra` field (JSON or dict). Lookups for `required` and `parameters` first check `credentials`, then `extra`.

2.  **Loading (`load` method):**
    *   Called with a `credentials` dictionary.
    *   Builds the final SQLAlchemy connection string using `DB_CONFIG` and `credentials` (including authentication handling).
    *   Creates the SQLAlchemy engine (`self.engine`) and connection (`self.connection`).

3.  **Executing Queries (`run_query` method):**
    *   Takes a SQL query string and optional `batch_size`.
    *   Executes the query using the established connection.
    *   Yields results in batches (lists of dictionaries).

### Example `DB_CONFIG`

```python
# In your subclass definition (e.g., my_connector/clients.py)
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.clients.models import DatabaseConfig

class SnowflakeClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="snowflake://{username}:{password}@{account_id}",
        required=["username", "password", "account_id"],
        parameters=["warehouse", "role"],
        defaults={"client_session_keep_alive": "true"},
        connect_args={"sslmode": "require"},  # Optional: driver-specific connection arguments
    )
```

### Interaction with Tasks

`BaseSQLClient` establishes the connection and holds the SQLAlchemy engine, which is used by `@task` methods to execute queries.

*   **Role of `SQLClient`:** Creates and manages the underlying database connection (`self.engine`) based on `DB_CONFIG` and credentials. Provides the configured engine and the `run_query` / `execute_query` methods to other components.
*   **Role of `@task` methods:**
    *   Tasks (e.g., `fetch_tables`, `fetch_columns` in your `SqlMetadataExtractor` subclass) orchestrate the extraction process.
    *   They create a client instance and call `load()` with credentials.
    *   They call methods on the client (like `execute_query`) to run queries and get data.
    *   They process the resulting data (e.g., pass to asset mappers for transformation).

**Simplified Flow:**
`@task method` -> creates `SQLClient` -> calls `client.load(credential_ref=...)` -> calls `client.execute_query(query=...)` -> receives data -> maps via asset mapper.

## Base Client (`base.py`)

Provides a base implementation for clients that need to connect to non-SQL data sources with methods for HTTP GET and POST requests.

### Key Classes

*   **`BaseClient(ClientInterface)`**:
    *   **Purpose:** Handles HTTP-based connections and request execution for non-SQL data sources. Provides a foundation for building clients that interact with REST APIs, NoSQL databases, or other HTTP-based services.
    *   **HTTP Support:** Built-in support for HTTP GET and POST requests with configurable headers, authentication, and retry logic.
    *   **Extensibility:** Designed to be subclassed for specific non-SQL data sources.

### Configuration and Usage

The `BaseClient` class is typically **subclassed** for specific non-SQL data sources (e.g., REST APIs) rather than used directly.

1.  **HTTP Configuration:**
    *   **`http_headers` (HeaderTypes):** HTTP headers for all requests made by this client. Supports dict, Headers object, or list of tuples. GET and POST requests through the `execute_http_get_request` and `execute_http_post_request` methods will use this header and allow for override through the `headers` parameter.
    *   **`http_retry_transport` (httpx.AsyncBaseTransport):** HTTP transport for requests. Uses httpx default transport by default, but can be overridden for custom retry behavior from libraries like `httpx-retries`.

2.  **Loading (`load` method):**
    *   Called with credentials and other configuration parameters.
    *   Should be implemented by subclasses to set up authentication headers and any required client state.
    *   Can optionally override `http_retry_transport` for advanced retry logic.

3.  **HTTP Request Methods:**
    *   **`execute_http_get_request()`:** Performs HTTP GET requests with configurable headers, parameters, and authentication.
    *   **`execute_http_post_request()`:** Performs HTTP POST requests with support for various data formats (JSON, form data, files, etc.).

### Example `BaseClient` Subclass

```python
# In your subclass definition (e.g., my_connector/clients.py)
from typing import Dict, Any
from application_sdk.clients.base import BaseClient

class MyApiClient(BaseClient):
    async def load(self, **kwargs: Any) -> None:
        """Initialize the client with credentials and set up HTTP headers."""
        credentials = kwargs.get("credentials", {})

        # Set up authentication headers
        self.http_headers = {
            "Authorization": f"Bearer {credentials.get('api_token')}",
            "User-Agent": "MyApp/1.0",
            "Content-Type": "application/json"
        }

        # Optionally set up custom retry transport for advanced retry logic
        # from httpx_retries import Retry, RetryTransport
        # retry = Retry(total=5, backoff_factor=10, status_forcelist=[429, 500, 502, 503, 504])
        # self.http_retry_transport = RetryTransport(retry=retry)

    async def fetch_data(self, endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Custom method to fetch data from the API."""
        response = await self.execute_http_get_request(
            url=f"https://api.example.com/{endpoint}",
            params=params
        )
        if response and response.status_code == 200:
            return response.json()
        return {}

    async def create_resource(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Custom method to create a resource via POST."""
        response = await self.execute_http_post_request(
            url=f"https://api.example.com/{endpoint}",
            json_data=data
        )
        if response and response.status_code == 201:
            return response.json()
        return {}
```

### Advanced Retry Configuration

For applications requiring advanced retry logic (e.g., status code-based retries, rate limiting, custom backoff strategies), you can use the `httpx-retries` library:

```python
class MyApiClient(BaseClient):
    async def load(self, **kwargs: Any) -> None:
        # Set up headers
        self.http_headers = {"Authorization": f"Bearer {kwargs.get('token')}"}

        # Install httpx-retries: pip install httpx-retries
        from httpx_retries import Retry, RetryTransport

        # Configure retry for status codes and network errors
        retry = Retry(
            total=5,
            backoff_factor=10,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        self.http_retry_transport = RetryTransport(retry=retry)
        # The RetryTransport can be overridden with a custom transport from libraries like `httpx-retries` through methods like `_retry_operation_async`. Check the library for more details.
```

## Temporal / Workflow Client (`temporal.py`, `workflow.py`, `utils.py`)

Provides clients for interacting with the Temporal workflow orchestration service.

### Key Classes

*   **`TemporalClient` (`temporal.py`)**:
    *   **Purpose:** Manages the low-level connection to the Temporal server frontend service.
    *   **Usage:** Typically instantiated *internally* by `TemporalWorkflowClient`.
*   **`WorkflowClient` (`workflow.py`)**:
    *   **Purpose:** An *abstract base class* defining the interface for interacting with *any* workflow engine (`start_workflow`, `stop_workflow`, etc.).
*   **`TemporalWorkflowClient(WorkflowClient)` (`temporal.py`)**:
    *   **Purpose:** The concrete *Temporal implementation* of `WorkflowClient`. Primary client for applications.
    *   **Connection:** Internally creates and uses a `TemporalClient` instance.
    *   **Configuration:** Initialized with `host`, `port`, `application_name`, `namespace`. Defaults read from environment variables.
    *   **Key Methods:** `load()`, `close()`, `start_workflow()`, `stop_workflow()`, `get_workflow_run_status()`, `create_worker()`.

### Prometheus Metrics

`create_temporal_client()` automatically exposes ~40 built-in Temporal SDK metrics via a Prometheus endpoint on every worker. No code changes are required — metrics become available as soon as the client is created.

**Endpoint:** `0.0.0.0:9464` by default (OpenTelemetry Prometheus convention).

**Override:** Set `ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS=<host>:<port>` to change the bind address.

**Sample metrics exposed:**

| Metric | Description |
|--------|-------------|
| `temporal_activity_execution_latency` | Activity execution duration |
| `temporal_activity_schedule_to_start_latency` | Time from schedule to start for activities |
| `temporal_workflow_completed` | Total completed workflows |
| `temporal_workflow_endtoend_latency` | End-to-end workflow duration |
| `temporal_request_latency` | gRPC request latency to Temporal server |
| `temporal_request_failure` | gRPC request failures |
| `temporal_worker_task_slots_available` | Available worker task slots |
| `temporal_worker_task_slots_used` | In-use worker task slots |
| `temporal_sticky_cache_hit` | Sticky cache hit count |
| `temporal_sticky_cache_size` | Current sticky cache size |

The Temporal `Runtime` that binds the metrics port is a process-level singleton (`_prometheus_runtime` in `backend.py`). Creating multiple client instances or calling `create_temporal_client()` more than once within the same process reuses the same `Runtime` and does not attempt to rebind the port.

### Configuration and Usage

The common pattern is to use the `get_workflow_client` utility function.

1.  **Getting a Client (`utils.py`)**:
    *   `get_workflow_client(engine_type=WorkflowEngineType.TEMPORAL, application_name=APPLICATION_NAME)` returns an instance of `TemporalWorkflowClient`.
    *   `application_name` determines the default Temporal `task_queue`.

2.  **Connecting (`load` method):** Must be called after instantiation.

3.  **Starting Workflows (`start_workflow` method):**
    *   Takes `workflow_args` (dict) and the `workflow_class`.
    *   Handles storing configuration/credentials securely (StateStore/SecretStore).
    *   Initiates the workflow execution on Temporal.

## Summary

The `clients` module abstracts interactions with external services.

`SQLClient` subclasses (configured via `DB_CONFIG`) provide the database engine and query execution methods, which are used by activities to fetch data. `TemporalWorkflowClient` (obtained via `get_workflow_client`) manages interactions with the Temporal service for workflow lifecycle management.

`BaseClient` provides a foundation for non-SQL data sources with HTTP request support through the `execute_http_get_request` and `execute_http_post_request` methods. The class also allows for custom retry logic to be configured through the `http_retry_transport` attribute which can be set to a `httpx.AsyncBaseTransport` instance, either through the `httpx` default transport or a custom transport from libraries like `httpx-retries`.