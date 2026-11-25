# PaaS Components

This folder contains Dapr PaaS components for various services.

## Requirements
- No Dapr CLI needed. The `daprd` runtime is fetched and pinned by the SDK
  (`application_sdk.dev._dapr`) and invoked directly — `uv run poe start-dapr`
  resolves and launches it for you.

## Where these files are used
This folder ships inside the `atlan-application-sdk` wheel as
`application_sdk/components/`. Consumer apps copy it into their own
`components/` (their `download-components` poe task), and `uv run poe start-dapr`
in this repo points `daprd` at it.

It is **not** the set the SDK generates for itself. The embedded runtime
(`application_sdk.dev._dapr`), the external-runtime path of the
`connector-integration-tests` action, and the `sdr-e2e` action all render
their components from `application_sdk/dev/_dapr_components.py`. Those use an
in-memory state store; this folder uses SQLite so local state survives a
restart. The objectstore and eventstore roots are shared with the generated
set, and `tests/unit/dev/test_dapr_components.py` enforces that.

## Local Development
1. State store - Uses SQLite as the state store at `./local/dapr/statestore.db`
2. Object store - Uses local file system at `./local/dapr/objectstore`
3. Secret store - Uses system environment variables

## Testing Dapr components only
- Run the dapr sidecar manually with `uv run poe start-dapr` (launches `daprd`
  directly against the YAMLs in this folder; no Dapr CLI involved)
- Test the state store components - Uses SQLite as the state store
    - Save state
```bash
curl -X POST -H "Content-Type: application/json" -d '[{ "key": "name", "value": "Bruce Wayne"}]' http://localhost:3500/v1.0/state/statestore`
```
- Get state
```bash
curl http://localhost:3500/v1.0/state/statestore/name`
```
- Delete state
```bash
curl -X DELETE http://localhost:3500/v1.0/state/statestore/name`
```
- Test the [object store bindings](https://docs.dapr.io/reference/components-reference/supported-bindings/localstorage/) - Uses local file system
    - Create file in `./local/dapr/objectstore/my-test-file.txt`
```bash
curl -d '{ "operation": "create", "data": "Hello World", "metadata": { "fileName": "my-test-file.txt" } }' \
      http://localhost:3500/v1.0/bindings/objectstore
```
- Test the secret store component - Uses system environment variables
    - Get secret
```bash
   curl http://localhost:3500/v1.0/secret/secretstore/HOMEBREW_CELLAR
```