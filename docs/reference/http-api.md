# HTTP API Reference

The Application SDK handler exposes a FastAPI HTTP service. All endpoints below are registered by `create_app_handler_service()` in `application_sdk/handler/service.py`.

Base URL (local): `http://localhost:8000`

---

## Handler Endpoints

### `POST /workflows/v1/auth`

Test connectivity and authentication with the target system.

**Request body:**
```json
{
  "credentials": [
    { "key": "host", "value": "db.example.com" },
    { "key": "username", "value": "admin" },
    { "key": "password", "value": "secret" }
  ]
}
```

**Response:**
```json
{
  "data": { "status": "success", "message": "Connection successful" },
  "success": true,
  "message": "Authentication success"
}
```

`status` values: `success`, `failed`, `expired`, `invalid_credentials`. All non-success values return HTTP 401.

Delegates to `Handler.test_auth(AuthInput)`.

---

### `POST /workflows/v1/check`

Run preflight checks (connectivity, permission, schema access).

**Request body:** Same shape as `/auth` (credentials list).

**Response:**
```json
{
  "data": {
    "authenticationCheck": { "success": true, "message": "Authenticated" },
    "permissionCheck": { "success": true, "message": "Read permission confirmed" }
  },
  "success": true,
  "message": "Preflight check ready"
}
```

Each check in `PreflightOutput.checks` is mapped to a key in `data` by lower-casing the first character of the check name (e.g. `"AuthCheck"` → `"authCheck"`). Names are not fully camelCased; spaces and inner capitals are preserved.

Delegates to `Handler.preflight_check(PreflightInput)`.

---

### `POST /workflows/v1/metadata`

Fetch metadata objects (databases, schemas, APIs, etc.) for use in the UI selection tree.

**Request body:** Same shape as `/auth` (credentials list).

**Response:**
```json
{
  "data": [
    { "name": "prod_db", "type": "database", "children": [...] }
  ],
  "success": true,
  "message": "Fetched 3 objects"
}
```

Delegates to `Handler.fetch_metadata(MetadataInput)`.

---

## Workflow Lifecycle

### `POST /workflows/v1/start`

Start a workflow run.

**Query param:** `?entrypoint=<name>` (optional). When omitted, the default `run()` entry point is used for single-entrypoint apps; apps with multiple `@entrypoint` methods return HTTP 400 if this parameter is absent. When set, it selects a specific `@entrypoint`-decorated method.

**Request body:**
```json
{
  "credential_guid": "abc-123",
  "connection": {
    "connection_name": "my-db",
    "connection_qualified_name": "default/postgres/1234567890"
  }
}
```

Example with entrypoint:
```
POST /workflows/v1/start?entrypoint=extract-metadata
```

> **Note:** `workflow_type` body field is supported as a deprecated fallback (planned removal in v3.1.0). Always use `?entrypoint=` for new code.

**Response:**
```json
{
  "success": true,
  "message": "Workflow started successfully",
  "data": {
    "workflow_id": "my-connector-abc123",
    "run_id": "run-xyz"
  },
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

`correlation_id` is echoed from the caller-supplied `correlation_id` body field if present; otherwise a new UUID is generated. Use it to correlate logs and traces across services.

---

### `POST /workflows/v1/stop/{workflow_id}/{run_id}`

Request graceful termination of a running workflow.

**Path params:** `workflow_id`, `run_id` (supports slashes — use URL encoding).

**Response:** `{ "success": true }`

---

### `GET /workflows/v1/status/{workflow_id}/{run_id}`

Poll workflow execution status.

**Response:**
```json
{
  "data": {
    "status": "RUNNING",
    "workflow_id": "my-connector-abc123",
    "run_id": "run-xyz",
    "execution_duration_seconds": 42
  }
}
```

`status` values are raw Temporal status names (uppercase): `RUNNING`, `COMPLETED`, `FAILED`, `CANCELED`, `TERMINATED`, `TIMED_OUT`, `UNKNOWN`.

> **Note:** `/status` returns uppercase Temporal state names; `/result` (below) returns its own lowercase normalized values. The two endpoints have different casing conventions.

---

### `GET /workflows/v1/result/{workflow_id}`

Fetch the most recent run result for a workflow.

**Query params:** `wait` (bool, default `false`) — when `true`, blocks until the workflow reaches a terminal state before returning.

**Response (completed):**
```json
{
  "data": {
    "status": "completed",
    "workflow_id": "my-connector-abc123",
    "result": { "record_count": 1234 }
  }
}
```

`status` values in the response body: `running`, `completed`, `failed`, `result_decode_failed`. Temporal's `CANCELED`, `TERMINATED`, `TIMED_OUT` states all map to `failed` in the response. When `wait=false` and the workflow is still running, the body has a `message` field instead of `result`. On failure the body has an `error` field instead of `result`.

---

## Configuration Endpoints

### `GET /workflows/v1/config/{config_id}`

Retrieve a named configuration object from the state store.

**Query params:** `type` (string, default `"workflows"`) — namespace key used to scope the config in the state store.

### `POST /workflows/v1/config/{config_id}`

Store a named configuration object.

**Query params:** `type` (string, default `"workflows"`) — namespace key. Using `type=workflows` is deprecated; pass a specific config type instead.

**Request body:** Any JSON object.

---

### `GET /workflows/v1/configmap/{config_map_id}`

Retrieve a generated configmap JSON (from `app/generated/{id}.json`).

**Error responses:**

- `404 {"detail": "ConfigMap '<id>' not found"}` — no matching file exists under `app/generated/`.

### `GET /workflows/v1/configmaps`

List all available configmap IDs.

---

### `GET /workflows/v1/manifest`

Return the Automation Engine DAG manifest (from `app/generated/manifest.json`).

> **Legacy alias:** An unversioned `GET /manifest` route is also registered for backward compatibility with older AE clients (`include_in_schema=false`). New callers should use `/workflows/v1/manifest`; the unversioned alias is scheduled for removal (BLDX-804).

**Query param:** `?entrypoint=<name>` (optional). When provided, returns the per-entry-point manifest from `app/generated/<name>/manifest.json`. When omitted, falls back to the root `app/generated/manifest.json`.

**Response:** `AppManifest` JSON — see [Multi-App Coordination](../guides/multi-app-coordination.md).

---

## File Endpoints

### `POST /workflows/v1/file`

Upload a file to be used as workflow input (e.g. CSV for file-based connectors).

**Request body:** `multipart/form-data` with a `file` field.

**Response:** `FileUploadResponse` serialized with camelCase aliases:
```json
{
  "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "version": "1",
  "isActive": true,
  "fileName": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4.csv",
  "rawName": "upload.csv",
  "key": "workflow_file_upload/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4/upload.csv",
  "extension": "csv",
  "contentType": "text/csv",
  "fileSize": 1234,
  "isUploaded": true,
  "uploadedAt": "2025-01-01T10:00:00Z",
  "createdAt": 1735729200000,
  "updatedAt": 1735729200000
}
```

---

## Event Endpoints

### `GET /dapr/subscribe`

Returns the Dapr pub/sub subscription configuration. Called by the Dapr sidecar on startup.

### `POST /events/v1/event/{event_id}`

Receive a Dapr cloud event. Routes to the appropriate `@on_event` handler.

### `POST /events/v1/drop`

Returns a Dapr `DROP` status, instructing the sidecar to drop the event without retry or dead-lettering.

---

## Development Endpoints

### `POST /workflows/v1/dev/local-vault`

Provision credentials into the local in-memory vault for local development. Used by `run_dev_combined()`.

**Request body:** Raw credential dict (same fields as the `credentials` array, but as a flat dict).

**Response:**
```json
{ "credential_guid": "dev-abc123" }
```

This endpoint requires `ATLAN_DEPLOYMENT_NAME=local` — requests with any other deployment name receive HTTP 403. It should never be exposed in production.

---

## Health and Observability

### `GET /health` · `GET /server/health`

Handler liveness check. Returns `200 OK` unconditionally when the FastAPI process is responding — this is a process-up probe, not a dependency-health check. No downstream services (Temporal, Dapr, Redis) are verified.

```json
{ "status": "healthy" }
```

### `GET /ready` · `GET /server/ready`

Handler readiness check. Returns `200 OK` with `{"status": "ok"}` when the process is up.

### `GET /metrics`

Prometheus metrics endpoint (application-level metrics). Available when `ATLAN_ENABLE_PROMETHEUS_METRICS=true` (default: `true`).

### `GET /`

Serves the custom frontend `index.html` when present at `ATLAN_FRONTEND_ASSETS_PATH`. Returns a JSON placeholder when no frontend bundle is found.

---

## Temporal Prometheus Metrics

A second Prometheus endpoint (Temporal SDK built-in metrics) is available separately at `http://host:9464/metrics`. See [Monitoring](../concepts/monitoring.md).

---

## Response Envelope

Most endpoints wrap their payload in a standard envelope:

```json
{
  "data": { ... },       // endpoint-specific payload
  "success": true,       // boolean overall success
  "message": "..."       // human-readable status message
}
```

Error responses follow standard HTTP status codes with a `detail` field in the body.
