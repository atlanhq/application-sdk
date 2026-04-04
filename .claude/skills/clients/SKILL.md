---
name: clients
description: Build HTTP or SQL clients for external data sources using the three-tier client hierarchy
user-invocable: true
---

# Build a Client

## Process

1. Read `docs/decisions/0003-client-inheritance-hierarchy.md` for the inheritance pattern
2. Determine if the target is an HTTP API or SQL database
3. For HTTP clients:
   - Extend `BaseClient` from `application_sdk/clients/base.py`
   - Override `load()` to set authentication headers and retry transport
   - Use `execute_http_get_request()` / `execute_http_post_request()` for requests
   - Return `None` on error (framework convention), log details via structured logger
4. For SQL clients:
   - Extend `BaseSQLClient` from `application_sdk/clients/sql.py`
   - Implement `get_sqlalchemy_connection_string()` for the target database engine
   - Handle auth modes: basic, IAM user, IAM role
   - Use `run_query()` for streaming results, `get_results()` for single fetches
5. Add type hints on all params and returns
6. Run `uv run pre-commit run --files <file>` to validate
7. Create tests at `tests/unit/clients/test_<name>.py`

## Key References

- `application_sdk/clients/base.py` — `ClientInterface`, `BaseClient`
- `application_sdk/clients/sql.py` — `BaseSQLClient`, `AsyncBaseSQLClient`
- `application_sdk/clients/atlan.py` — authentication flow patterns

## Handling `$ARGUMENTS`

If arguments specify a data source name, use it as the module and class name. If they specify HTTP or SQL, skip the determination step.
