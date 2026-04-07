---
name: handlers
description: Implement handlers bridging clients and activities
user-invocable: true
---

# Build a Handler

## Process

1. Read `application_sdk/handlers/__init__.py` for the `HandlerInterface` contract
2. Create a handler class inheriting from `HandlerInterface`
3. Implement the four required methods:
   - `load()` — Create and initialize the client with credentials from the config
   - `test_auth()` — Make a lightweight call to verify credentials work
   - `preflight_check()` — Validate permissions, connectivity, and prerequisites
   - `fetch_metadata()` — Retrieve metadata from the data source using the client
4. Override `get_configmap()` if the handler needs custom configuration beyond contract-generated JSON
5. The handler's configmap follows K8s format: `metadata.name`, `kind`, `apiVersion`, `data.config`
6. Add type hints on all methods
7. Run pre-commit and create tests

## Key References

- `application_sdk/handlers/__init__.py` — `HandlerInterface`, configmap loading, credential management

## Handling `$ARGUMENTS`

If arguments specify the data source or client type, wire up the appropriate client in `load()` and tailor `fetch_metadata()` to that source's API.
