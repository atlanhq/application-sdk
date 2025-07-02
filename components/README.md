# PaaS Components

This folder contains Dapr PaaS components for various services.

## Requirements
- Install [Dapr CLI](https://docs.dapr.io/getting-started/install-dapr-cli/)

## Local Development
1. State store - Uses SQLite as the state store at `./local/dapr/statestore.db`
2. Object store - Uses local file system at `./local/dapr/objectstore`
3. Secret store - Uses system environment variables

## Testing Dapr components only
- Run the dapr sidecar manually `make dapr-sidecar`
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