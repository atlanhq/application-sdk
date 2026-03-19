---
name: services
description: Use ObjectStore, StateStore, SecretStore, and EventStore via Dapr
user-invocable: true
---

# Use SDK Services

## Process

1. Identify which service you need:
   - **ObjectStore** — Upload/download files, list objects, manage artifacts
   - **StateStore** — Read/write workflow state and credentials (JSON objects)
   - **SecretStore** — Resolve credentials from Dapr secret store or direct config
   - **EventStore** — Publish application lifecycle events
2. Import from `application_sdk/services/`
3. Key patterns:
   - **ObjectStore**: Path params (key, prefix) accept both `./local/tmp/...` and `artifacts/...` — normalized internally via `as_store_key()`. Local file params (`source`, `destination`) are NOT normalized.
   - **StateStore**: Uses `StateType.WORKFLOWS` or `StateType.CREDENTIALS` enum. `save_state()` merges into existing state; `save_state_object()` replaces entirely.
   - **SecretStore**: Supports DIRECT (credentials in state) and AGENT (fetched from secret store) modes. MULTI_KEY fetches entire bundle; SINGLE_KEY fetches per-field.
   - **EventStore**: Publish events with `EventMetadata` for consistent structure.
4. All services use Dapr components defined in `application_sdk/constants.py`
5. Check `DAPR_MAX_GRPC_MESSAGE_LENGTH` (100MB) for large file uploads

## Key References

- `application_sdk/services/objectstore.py` — `ObjectStore`, `as_store_key()`
- `application_sdk/services/statestore.py` — `StateStore`, `StateType`
- `application_sdk/services/secretstore.py` — `SecretStore`, credential resolution
- `application_sdk/constants.py` — Dapr component names, path templates

## Handling `$ARGUMENTS`

If arguments specify a service or operation, focus guidance on that specific service's API and patterns.
