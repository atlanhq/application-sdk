# Create Client

Build a new HTTP or SQL client for an external data source.

## Source of Truth

Read these before starting:
- `docs/decisions/0003-client-inheritance-hierarchy.md` — three-tier client hierarchy
- `docs/agents/coding-standards.md` — formatting, naming, type hints

## Reference Implementations

Study these for patterns:
- `application_sdk/clients/base.py` — `ClientInterface`, `BaseClient` (HTTP)
- `application_sdk/clients/sql.py` — `BaseSQLClient`, `AsyncBaseSQLClient` (SQL)
- `application_sdk/clients/atlan.py` — authentication flow example

## Instructions

1. **Determine client type**: HTTP (`BaseClient`) or SQL (`BaseSQLClient`) based on the data source
2. **Create file** at `application_sdk/clients/<source_name>.py`
3. **Implement the class**:
   - For HTTP: extend `BaseClient`, override `load()` to set headers and auth, optionally override `http_retry_transport` for custom retry logic
   - For SQL: extend `BaseSQLClient`, implement `get_sqlalchemy_connection_string()`, handle auth modes (basic, IAM user, IAM role)
4. **Add type hints** on all params and return types
5. **Add docstrings** to the class and public methods
6. **Run pre-commit**: `uv run pre-commit run --files application_sdk/clients/<source_name>.py`
7. **Create tests** at `tests/unit/clients/test_<source_name>.py` — mock external calls, test auth flows, test error handling

$ARGUMENTS
