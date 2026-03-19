# 0003: Client Inheritance Hierarchy

**Status**: Accepted

## Context

The SDK connects to diverse external data sources — REST APIs, SQL databases, and the Atlan platform itself. Each connection type has different authentication, transport, and error handling needs, but shares common patterns like lazy initialization and credential management.

## Decision

### Three-tier hierarchy

```
ClientInterface (abstract)
├── BaseClient (HTTP)
│   ├── Custom HTTP clients (override load() for headers/retry)
│   └── AtlanClient (special auth flow)
└── BaseSQLClient (SQL)
    ├── Sync SQL clients (SQLAlchemy engine)
    └── AsyncBaseSQLClient (async SQLAlchemy engine)
```

### `ClientInterface`

Abstract base defining the contract: an optional async `load()` method for initialization. All clients implement this interface.

### `BaseClient` (HTTP)

Provides HTTP transport via `httpx.AsyncClient` with:
- Two-level header management (client-level + method-level, merged via `httpx.Headers`)
- `execute_http_get_request()` and `execute_http_post_request()` with auth, timeout, and redirect support
- Customizable retry transport via `http_retry_transport` property (override in `load()`)
- Error handling that returns `None` on failure and logs `HTTPStatusError` details

HTTP clients customize behavior by overriding `load()` to set headers, auth tokens, and retry policies.

### `BaseSQLClient` (SQL)

Extends the hierarchy for database connections:
- `get_sqlalchemy_connection_string()` constructs engine-specific URLs
- Multiple auth modes: Basic, IAM User (AWS RDS), IAM Role (with external ID)
- Lazy connections — `load()` creates the engine but connections are created on-demand
- Batch query execution: `run_query()` (generator), `get_batched_results()` (async DataFrames), `get_results()` (single DataFrame)
- Optional server-side cursors via `USE_SERVER_SIDE_CURSOR` for large result sets

## Consequences

- **Pro**: Clear separation — HTTP clients never deal with SQL, SQL clients get connection string building for free
- **Pro**: Adding a new data source typically requires only overriding `load()` and a few config methods
- **Con**: Two parallel hierarchies (sync/async SQL) require maintaining parity
- **Con**: `BaseClient` returning `None` on error (instead of raising) requires callers to check return values

## References

- `application_sdk/clients/base.py` — `ClientInterface`, `BaseClient`
- `application_sdk/clients/sql.py` — `BaseSQLClient`, `AsyncBaseSQLClient`
- `application_sdk/clients/atlan.py` — `get_client()`, `get_async_client()`
