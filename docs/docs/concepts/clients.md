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
    *   This dictionary **must** be defined in your `BaseSQLClient` subclass to specify how to connect.
    *   **`template` (str):** The SQLAlchemy connection string template. Uses standard Python f-string formatting with placeholders for keys defined in `required` (e.g., `{username}`, `{host}`).
    *   **`required` (list[str]):** List of keys that *must* be present in the `credentials` dictionary passed to `load()`. The client fetches values for these keys from `credentials` to format the `template`. The value for the `{password}` placeholder is handled specially by `get_auth_token()` based on `authType`.
    *   **`parameters` (list[str], optional):** List of optional keys. If present in `credentials`, their values are fetched and appended as URL query parameters to the connection string (e.g., `?warehouse=my_wh&role=my_role`).
    *   **`defaults` (dict, optional):** Default key-value pairs to append as URL query parameters if they are *not* found in `credentials`.
    *   **Note on Credentials:** The `credentials` dictionary passed to `load()` can also contain an `extra` field (often a JSON string) which is parsed. Values for `required` and `parameters` keys are looked up first directly in `credentials`, then within the parsed `extra` dictionary. `authType` (e.g., "basic", "iam_user", "iam_role") is also read from `credentials` to determine how to handle the `password`.

2.  **Loading (`load` method):**
    *   Called with a `credentials` dictionary.
    *   Builds the final SQLAlchemy connection string using `DB_CONFIG` and `credentials` (including authentication handling).
    *   Creates the SQLAlchemy engine (`self.engine`) and connection (`self.connection`).

3.  **Executing Queries (`run_query` method):**
    *   Takes a SQL query string and optional `batch_size`.
    *   Executes the query using the established connection.
    *   Yields results in batches (lists of dictionaries). This method is useful for direct execution but often less used than `SQLQueryInput` within activities.

### Example `DB_CONFIG`

```python
# In your subclass definition (e.g., my_connector/clients.py)
from typing import Dict, Any
from application_sdk.clients.sql import BaseSQLClient

class SnowflakeClient(BaseSQLClient):
    DB_CONFIG: Dict[str, Any] = {
        # Template uses required keys
        "template": "snowflake://{username}:{password}@{account_id}",
        # Values for these are fetched from credentials or credentials['extra']
        "required": ["username", "password", "account_id"],
        # If 'warehouse' or 'role' exist in credentials/extra, they are added as ?warehouse=...&role=...
        "parameters": ["warehouse", "role"],
        # If 'client_session_keep_alive' is NOT in credentials/extra, add ?client_session_keep_alive=true
        "defaults": { "client_session_keep_alive": "true" }
    }
```

### Interaction with `SQLQueryInput` and Activities

While `BaseSQLClient` establishes the connection and holds the SQLAlchemy engine, the actual execution of queries *within standard activities* (like those for metadata or query extraction) is often delegated to `SQLQueryInput` (from `application_sdk.inputs`).

*   **Role of `SQLClient`:** Creates and manages the underlying database connection (`self.engine`) based on `DB_CONFIG` and credentials. Provides the configured engine to other components.
*   **Role of `SQLQueryInput`:**
    *   Takes the `engine` from the initialized `SQLClient` instance and a specific `query` string as input.
    *   Handles the execution of that single query against the provided engine.
    *   Provides methods like `get_daft_dataframe()`, `get_dataframe()`, `get_batched_dataframe()` to return the results conveniently as Daft or Pandas DataFrames, abstracting away the details of cursor handling and batch fetching for the activity developer.
*   **Role of Activities:**
    *   Activities (e.g., `fetch_tables`, `fetch_columns` in `BaseSQLMetadataExtractionActivities`) orchestrate the process.
    *   They retrieve the initialized `SQLClient` (and its `engine`) from the shared activity state.
    *   They instantiate `SQLQueryInput` with the client's engine and the appropriate SQL query (often defined as a class attribute on the activity or loaded from a file).
    *   They call methods on `SQLQueryInput` (like `get_daft_dataframe`) to get the data.
    *   They process the resulting DataFrame (e.g., save it to Parquet, transform it).

**Simplified Flow:**
`Activity` -> gets `SQLClient` from state -> creates `SQLQueryInput(engine=sql_client.engine, query=...)` -> calls `sql_query_input.get_daft_dataframe()` -> receives DataFrame -> processes DataFrame.

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

### Example (Getting and Using `TemporalWorkflowClient`)

```python
# In your application setup (e.g., examples/application_fastapi.py)
import asyncio
# Absolute imports
from application_sdk.clients.utils import get_workflow_client
from application_sdk.server.fastapi import Application, HttpWorkflowTrigger
# Assuming your custom classes are defined
from my_connector.handlers import MyConnectorHandler
from my_connector.workflows import MyConnectorWorkflow

async def run_app():
    # Get the workflow client using the utility function
    workflow_client = get_workflow_client(application_name="my-connector-queue")
    await workflow_client.load() # Connect to Temporal

    # Instantiate the FastAPI application, passing the connected client
    fast_api_app = APIServer(
        handler=MyConnectorHandler(),
        workflow_client=workflow_client
    )

    # Register workflow triggers
    fast_api_app.register_workflow(
        MyConnectorWorkflow,
        [HttpWorkflowTrigger(endpoint="/start", methods=["POST"])]
    )

    # Start the application server
    await fast_api_app.start()
    # await workflow_client.close() # Handle on shutdown

if __name__ == "__main__":
    asyncio.run(run_app())
```

## Summary

The `clients` module abstracts interactions with external services.

`SQLClient` subclasses (configured via `DB_CONFIG`) provide the database engine, which is then typically used by `SQLQueryInput` within activities to fetch data as DataFrames. `TemporalWorkflowClient` (obtained via `get_workflow_client`) manages interactions with the Temporal service for workflow lifecycle management.

`BaseClient` provides a foundation for non-SQL data sources with HTTP request support through the `execute_http_get_request` and `execute_http_post_request` methods. The class also allows for custom retry logic to be configured through the `http_retry_transport` attribute which can be set to a `httpx.AsyncBaseTransport` instance, either through the `httpx` default transport or a custom transport from libraries like `httpx-retries`.