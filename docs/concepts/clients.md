# Clients

This module provides the necessary abstractions (clients) for interacting with various external systems required by applications, such as databases and HTTP-based services.

## Core Concepts

1.  **`ClientInterface` (`application_sdk.clients`)**:
    *   **Purpose:** An abstract base class defining the minimal contract for all clients. It requires implementing an `async def load()` method for connection/setup and provides an optional `async def close()` for cleanup.
    *   **Extensibility:** Any class interacting with an external service should ideally inherit from this interface.

2.  **Specialized Clients:** The SDK provides concrete client implementations for specific services:
    *   **SQL Databases (`sql.py`):** For connecting to and querying SQL databases.
    *   **Non-SQL Systems (`base.py`):** For connecting to non-SQL data sources like REST APIs, or other services.

## SQL Client (`sql.py`)

Provides classes for interacting with SQL databases using SQLAlchemy.

### Key Classes

*   **`BaseSQLClient(ClientInterface)`**:
    *   **Purpose:** Handles synchronous connections and query execution using SQLAlchemy's standard engine and connection pool. Good for `@task` methods or setup steps that don't require high concurrency within the client itself.
    *   **Query Execution:** Dispatches `run_query` via `loop.run_in_executor` with a private `ThreadPoolExecutor` so the event loop (and the framework's auto-heartbeat task) keeps running during synchronous query execution.
*   **`AsyncBaseSQLClient(BaseSQLClient)`**:
    *   **Purpose:** Handles asynchronous connections and query execution using SQLAlchemy's async features (`create_async_engine`, `AsyncConnection`). Suitable for scenarios requiring non-blocking database I/O.
    *   **Query Execution:** Uses `async/await` directly with the async SQLAlchemy connection for `run_query`.

### Configuration and Usage

Both SQL client classes are typically **subclassed** for specific database types (e.g., PostgreSQL, Snowflake) rather than used directly.

1.  **Connection Configuration (`DB_CONFIG` - Class Attribute):**
    *   Define `DB_CONFIG` using the Pydantic model `DatabaseConfig` (`application_sdk.clients.DatabaseConfig`).
    *   **`template` (str):** SQLAlchemy connection string template using placeholders (e.g., `{username}`, `{host}`).
    *   **`required` (list[str]):** Keys that must be present in `credentials`/`credentials.extra`. `{password}` is resolved via `get_auth_token()` depending on `authType`.
    *   **`parameters` (list[str], optional):** Optional keys appended as URL query parameters when present in `credentials`/`extra`.
    *   **`defaults` (dict[str, Any], optional):** Default URL parameters always appended unless already in the template.
    *   **`connect_args` (dict[str, Any], optional):** Additional connection arguments to be passed directly to SQLAlchemy's `create_engine` or `create_async_engine`. Useful for driver-specific connection parameters that are not part of the connection URL. Defaults to `{}`.
    *   **`pool_pre_ping` (bool, optional):** Whether SQLAlchemy should test pooled connections for liveness before checkout. Defaults to `True` for backward compatibility; connector subclasses can set this to `False` when a dialect or endpoint has an unsafe ping/close path and the connector performs its own explicit validation query.
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
from application_sdk.clients import BaseSQLClient, DatabaseConfig

class SnowflakeClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="snowflake://{username}:{password}@{account_id}",
        required=["username", "password", "account_id"],
        parameters=["warehouse", "role"],
        defaults={"client_session_keep_alive": "true"},
        connect_args={},  # Optional: driver-specific connection arguments (e.g. {"connect_timeout": 30} for PostgreSQL)
        pool_pre_ping=True,  # Optional: disable only if the dialect's pre-ping path is unsafe
    )
```

### Interaction with Tasks

`BaseSQLClient` establishes the connection and holds the SQLAlchemy engine, which is used by `@task` methods to execute queries.

*   **Role of `BaseSQLClient`:** Creates and manages the underlying database connection (`self.engine`) based on `DB_CONFIG` and credentials. Provides the configured engine and the `run_query` method to other components.
*   **Role of `@task` methods:**
    *   Tasks (e.g., `fetch_tables`, `fetch_columns` in your `SqlMetadataExtractor` subclass) orchestrate the extraction process.
    *   They create a client instance and call `load()` with a credentials dict.
    *   They call `client.run_query(query)` to execute queries and yield row batches.
    *   They process the resulting data (e.g., pass to asset mappers for transformation).

**Simplified Flow:**
`@task method` → creates `BaseSQLClient` → `client.load(credentials=cred_dict)` → `client.run_query(query=...)` → yields row batches → asset mapper.

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
from application_sdk.clients import BaseClient

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

## Redis Client (`redis.py`)

`RedisClient` and `RedisClientAsync` are distributed-lock helpers — they implement a low-level acquire/release lock protocol via Redis. The internal lock primitives (`_acquire_lock`, `_release_lock`) are exposed by the higher-level `CapacityPool` abstraction, which is the recommended interface for managing concurrency slots across pods. See [State, Secrets, Pub/Sub & Bindings](state-secrets-pubsub.md#capacitypool).

Requires the `[distributed_lock]` extra: `uv add 'atlan-application-sdk[distributed_lock]'`.

Configuration env vars: `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`. See `docs/configuration.md` for the full list including sentinel and lock settings.

## Azure Client (`azure/`)

`application_sdk.clients.azure` provides an Azure-specific client layer:

- `AzureClient` (`azure/client.py`) — base client for Azure service interactions.
- `AzureAuthProvider` (`azure/auth.py`) — handles Azure Service Principal authentication.

Requires the `[azure]` extra: `uv add 'atlan-application-sdk[azure]'`.

```python
from application_sdk.clients import AzureClient, AzureAuthProvider
```

## SSL Utilities (`ssl_utils.py`)

`application_sdk.clients.ssl_utils` provides helpers for constructing SSL contexts from PEM certificates stored in a directory:

```python
from application_sdk.clients import create_ssl_context_with_custom_certs

# cert_dir must contain ca.crt, client.crt, client.key files
ssl_ctx = create_ssl_context_with_custom_certs(cert_dir="/etc/ssl/my-app")
```

Use `get_ssl_context()` to let the SDK discover the certificate directory automatically from the `SSL_CERT_DIR` environment variable:

```python
from application_sdk.clients import get_ssl_context

ssl_ctx = get_ssl_context()   # returns True (default cert verification) if SSL_CERT_DIR is not set
```

Pass the resulting `ssl.SSLContext` via `DB_CONFIG.connect_args={"ssl": ssl_ctx}` for mutual-TLS database connections.

## Prometheus Metrics

Every deployed application exposes its full metrics surface — SDK
custom metrics, HTTP server instrumentation, Temporal SDK Rust-core
families, and `prometheus_client` defaults — through a single FastAPI
`/metrics` endpoint on `containerPort` (default 8000). The Temporal
Rust-core endpoint at `127.0.0.1:9464` is loopback-only and proxied
in-process; operators don't scrape it directly.
`ATLAN_ENABLE_PROMETHEUS_METRICS` (default `true`) gates only that
loopback binding, not the FastAPI route. See
[`docs/concepts/monitoring.md`](monitoring.md) for the full
architecture, including the Pushgateway path used by worker pods in
split deployments.

## Summary

The `clients` module abstracts interactions with external services.

`BaseSQLClient` subclasses (configured via `DB_CONFIG`) provide the database engine and query execution methods used by `@task` methods to fetch data. See [`docs/concepts/apps.md`](apps.md) for how `App` + `@task` orchestrate extraction and [`docs/concepts/tasks.md`](tasks.md) for the task contract pattern.

Temporal worker lifecycle (startup, shutdown, workflow dispatch) is managed by `application_sdk.execution` — see [`docs/concepts/entry-points.md`](entry-points.md) for how workers are started and how entrypoints map to workflows.

`BaseClient` provides a foundation for non-SQL data sources with HTTP request support through the `execute_http_get_request` and `execute_http_post_request` methods. The class also allows for custom retry logic to be configured through the `http_retry_transport` attribute which can be set to a `httpx.AsyncBaseTransport` instance, either through the `httpx` default transport or a custom transport from libraries like `httpx-retries`.
