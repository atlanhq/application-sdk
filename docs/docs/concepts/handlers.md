# Handlers

This module provides the crucial link between the generic components of the Application SDK (like `Application` and `Activities`) and the specific logic required to interact with a particular target system (e.g., a specific database, API, or service). Handlers encapsulate this system-specific interaction logic.

## Purpose and Role of Handlers

Handlers serve as the core implementation layer for a connector. Their primary responsibilities include:

1.  **System Interaction Logic:** Containing the methods that perform actions specific to the target system, such as validating credentials, performing health checks, fetching metadata lists, or executing specific queries/API calls needed by activities.
2.  **Client Management:** Often managing the lifecycle (loading, closing) of the specific `ClientInterface` implementation needed to communicate with the target system.
3.  **API Implementation:** Providing the concrete implementation for the standard API endpoints exposed by `application_sdk.server.fastapi.Application` (like `/test_auth`, `/metadata`, `/preflight_check`).
4.  **Activity Logic Delegation:** Providing methods that activities can call (via the shared activity state) to perform tasks related to the target system.

Essentially, while `Clients` provide the *connection* abstraction, `Handlers` provide the *operational* abstraction for a specific data source type.

## `HandlerInterface` (`application_sdk.handlers.__init__.py`)

This is the abstract base class defining the contract that all handlers must fulfill. It ensures a consistent interface for the `Application` and potentially `Activities` to interact with any handler, regardless of the underlying system.

### Required Methods

Subclasses **must** implement the following asynchronous methods:

*   **`load(*args, **kwargs)`**:
    *   **Purpose:** Initialize the handler and any required resources, typically including loading the associated `ClientInterface` with credentials often passed via `kwargs`.
*   **`test_auth(*args, **kwargs)`**:
    *   **Purpose:** Verify that the provided credentials (usually in `kwargs`) are valid for authenticating against the target system. Should return `True` for success, `False` or raise an exception for failure.
    *   **Used By:** The `/workflows/v1/test_auth` endpoint in `Application`.
*   **`preflight_check(*args, **kwargs)`**:
    *   **Purpose:** Perform necessary checks before starting a main workflow execution. This often includes verifying connectivity, checking permissions, validating configuration (e.g., include/exclude filters passed via `kwargs`), or checking for the existence of required resources (like tables or schemas). Should return a dictionary summarizing the check results, typically indicating overall success and details for each check performed.
    *   **Used By:** The `/workflows/v1/preflight_check` endpoint in `Application` and often called as the first step in standard workflows.
*   **`fetch_metadata(*args, **kwargs)`**:
    *   **Purpose:** Fetch metadata from the target system. The specific type and format of metadata depend on the implementation and the arguments passed (e.g., fetching databases vs. schemas). Should return the fetched metadata, often as a list of dictionaries.
    *   **Used By:** The `/workflows/v1/metadata` endpoint in `Application`.

## `BaseSQLHandler` (`application_sdk.handlers.sql.py`)

This is a concrete implementation of `HandlerInterface` specifically designed for interacting with SQL-based data sources.

### Key Features

*   **Uses `SQLClient`:** It requires an instance of a `BaseSQLClient` subclass (passed during initialization) to communicate with the database.
*   **SQL Query Driven:** Much of its default behavior relies on executing predefined SQL queries.
*   **Configurable SQL Queries:** Provides class attributes to hold SQL query strings for common operations. These are often loaded from `.sql` files (using `read_sql_files` looking in `app/sql` by default) but can be overridden in subclasses:
    *   `test_authentication_sql`: Query to test connection/authentication (default: `SELECT 1;`).
    *   `client_version_sql`: Query to fetch the database client version (used in `check_client_version`).
    *   `metadata_sql`: Query to fetch database and schema names together (used by `prepare_metadata` which is called by the default `fetch_metadata` implementation).
    *   `tables_check_sql`: Query to count tables based on filters (used by `tables_check` within `preflight_check`).
    *   `fetch_databases_sql`: Query to fetch only database names.
    *   `fetch_schemas_sql`: Query to fetch schema names for a given database.
*   **Default Implementations:**
    *   `load()`: Calls `load()` on the provided `sql_client`.
    *   `test_auth()`: Executes `test_authentication_sql` using `SQLQueryInput`.
    *   `fetch_metadata()`: Based on the `metadata_type` argument, calls `prepare_metadata`, `fetch_databases`, or `fetch_schemas`. `prepare_metadata` executes `metadata_sql` using `SQLQueryInput`.
    *   `preflight_check()`: Orchestrates several checks:
        *   `check_schemas_and_databases()`: Executes `metadata_sql` and validates include/exclude filters against the results.
        *   `tables_check()`: Executes `tables_check_sql` (prepared with filters) to count tables.
        *   `check_client_version()`: Executes `client_version_sql` and checks against `SQL_SERVER_MIN_VERSION`.

## How Handlers are Used

1.  **By the `Application`:**
    *   When you create an instance of `application_sdk.server.fastapi.APIServer`, you pass your custom handler instance to its constructor (e.g., `app = APIServer(handler=MyConnectorHandler())`).
    *   The `APIServer`'s default API endpoints (`/test_auth`, `/metadata`, `/preflight_check`) directly call the corresponding methods (`test_auth`, `fetch_metadata`, `preflight_check`) on the provided handler instance to perform the actual work.

2.  **By `Activities`:**
    *   Standard activity base classes (like `BaseSQLMetadataExtractionActivities`) typically expect a specific handler type (e.g., `BaseSQLHandler`).
    *   During activity initialization (usually within the overridden `_set_state` method of the activity class), the appropriate handler is instantiated (often passing the corresponding client).
    *   This handler instance is stored in the `ActivitiesState` object associated with the workflow run.
    *   Activity methods then access this handler via `state.handler` (e.g., `state = await self._get_state(...)`, `await state.handler.some_custom_check()`) to execute system-specific logic required for that activity step.

## Extending Handlers

Creating a new connector requires creating a custom handler.

### 1. Subclassing `HandlerInterface` (for non-SQL sources)

If your target system is not SQL-based (e.g., a REST API, NoSQL database), inherit directly from `HandlerInterface` and implement all required abstract methods (`load`, `test_auth`, `fetch_metadata`, `preflight_check`) using the appropriate client for that system.

```python
# Example for a hypothetical REST API source
# In my_api_connector/handlers.py
from typing import Any, Dict, List
# Absolute imports
from application_sdk.handlers import HandlerInterface
# Assuming you have a client for this API
from .clients import MyApiClient

class MyApiHandler(HandlerInterface):
    api_client: MyApiClient

    async def load(self, **kwargs: Any) -> None:
        print("Loading MyApiHandler")
        self.api_client = MyApiClient()
        # Pass credentials from kwargs to the client's load/connect method
        await self.api_client.connect(credentials=kwargs.get("credentials", {}))

    async def test_auth(self, **kwargs: Any) -> bool:
        print("Testing API Authentication")
        # Use the client to validate credentials against the API
        return await self.api_client.check_token()

    async def preflight_check(self, **kwargs: Any) -> Any:
        print("Performing API Preflight Checks")
        # Use the client to check connectivity, permissions etc.
        ping_ok = await self.api_client.ping()
        read_perms_ok = await self.api_client.can_read_data()
        return {
            "success": ping_ok and read_perms_ok,
            "data": {
                 "connectivityCheck": {"success": ping_ok, "message": "API reachable"},
                 "permissionCheck": {"success": read_perms_ok, "message": "Read access verified"}
            }
        }

    async def fetch_metadata(self, **kwargs: Any) -> List[Dict[str, str]]:
        print("Fetching API Metadata")
        # Use the client to fetch metadata (e.g., list available datasets)
        # Example: metadata_type might be passed in kwargs
        if kwargs.get("metadata_type") == "datasets":
            return await self.api_client.list_datasets()
        else:
            return []

# Usage: Pass an instance of MyApiHandler to Application or Activities
```

### 2. Subclassing `BaseSQLHandler` (for SQL sources)

If your target is a SQL database, inherit from `BaseSQLHandler`. You'll typically need to:
*   Provide the appropriate `SQLClient` subclass during initialization.
*   Override specific methods (`test_auth`, `preflight_check`, etc.) if the default SQL-based logic isn't sufficient or needs modification.
*   Override SQL query class attributes (`test_authentication_sql`, `metadata_sql`, etc.) if the default queries (from `app/sql`) are incorrect for your specific SQL dialect.

```python
# Example for a specific SQL database (e.g., PostgreSQL)
# In my_postgres_connector/handlers.py
from typing import Dict, Any
# Absolute imports
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.observability.logger_adaptor import get_logger
# Import your specific SQL client
from .clients import PostgreSQLClient

logger = get_logger(__name__)

class MyPostgresHandler(BaseSQLHandler):
    # Override specific SQL queries if needed (or rely on defaults loaded from app/sql)
    # Example: Using a slightly different query for version check
    client_version_sql = "SELECT version();"

    # Constructor typically just passes the client to the base class
    def __init__(self, sql_client: PostgreSQLClient):
         # Ensure the correct client type is passed
         super().__init__(sql_client)

    # Override test_auth if the default 'SELECT 1;' isn't enough
    async def test_auth(self, **kwargs: Any) -> bool:
        logger.info("Running PostgreSQL specific test_auth")
        try:
            # Reuse the base class logic which runs test_authentication_sql
            # Or add more specific checks here if needed
            return await super().test_auth(**kwargs)
        except Exception as e:
            logger.error(f"PostgreSQL auth failed: {e}")
            return False

    # Override preflight_check if you need different checks or messages
    async def preflight_check(self, payload: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        logger.info("Running PostgreSQL specific preflight_check")
        # You could call super().preflight_check() and modify the result,
        # or implement entirely custom checks.
        # Example: Add a check for a specific extension
        base_checks = await super().preflight_check(payload, **kwargs)

        # Add custom check
        extension_check = await self.check_pg_extension("uuid-ossp")
        base_checks["extensionCheck"] = extension_check
        # Update overall success based on custom check
        base_checks["success"] = base_checks["success"] and extension_check["success"]

        if not base_checks["success"]:
             logger.warning("PostgreSQL preflight checks failed.")
             base_checks["error"] = "Preflight check failed (see details)" # Add/Modify error

        return base_checks

    async def check_pg_extension(self, extension_name: str) -> Dict[str, Any]:
        # Custom method using self.sql_client to run a query
        query = f"SELECT 1 FROM pg_extension WHERE extname = '{extension_name}';"
        try:
             async for batch in self.sql_client.run_query(query):
                 if batch: # If query returned rows, extension exists
                      return {"success": True, "message": f"Extension '{extension_name}' found."}
             # If no rows returned
             return {"success": False, "message": f"Required extension '{extension_name}' not found."}
        except Exception as e:
             logger.error(f"Error checking extension {extension_name}: {e}")
             return {"success": False, "message": f"Error checking extension: {e}"}

# Usage:
# 1. Instantiate your client: pg_client = PostgreSQLClient()
# 2. Instantiate your handler: pg_handler = MyPostgresHandler(pg_client)
# 3. Pass pg_handler to Application or use within Activities
```

## Summary

Handlers are essential components that contain the specific logic for interacting with a target data source. They implement the `HandlerInterface` contract and are used by both the `Application` (for standard API endpoints) and `Activities` (for workflow steps). For SQL sources, subclassing `BaseSQLHandler` provides a convenient starting point, while non-SQL sources require implementing `HandlerInterface` directly.