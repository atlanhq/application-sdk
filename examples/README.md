# Dapr Application Components

This repository contains Dapr components for various services.

## Requirements
- Install [Dapr CLI](https://docs.dapr.io/getting-started/install-dapr-cli/)

## Local Development
1. State store - Uses SQLite as the state store at `/tmp/dapr/statestore.db`
2. Object store - Uses local file system at `/tmp/dapr/objectstore`
3. Secret store - Uses system environment variables

## Testing Dapr components only
- Run the dapr sidecar manually `dapr run --app-id app --app-port 3000 --dapr-http-port 3500 --dapr-grpc-port 50001 --dapr-http-max-request-size 1024 --resources-path ./components`
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
    - Create file in `/tmp/dapr/objectstore/my-test-file.txt`
```bash
curl -d '{ "operation": "create", "data": "Hello World", "metadata": { "fileName": "my-test-file.txt" } }' \
      http://localhost:3500/v1.0/bindings/objectstore
```
- Test the secret store component - Uses system environment variables
    - Get secret
```bash
   curl http://localhost:3500/v1.0/secret/secretstore/HOMEBREW_CELLAR
```