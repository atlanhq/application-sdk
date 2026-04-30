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

`status` values: `success`, `failed`. HTTP status mirrors the auth result (200 / 401).

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

Each check in `PreflightOutput.checks` becomes a camelCase key in `data`.

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

**Query param:** `?entrypoint=<name>` (optional). When omitted, the default `run()` entry point is used. When set, it selects a specific `@entrypoint`-decorated method.

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
POST /workflows/v1/start?entrypoint=extract_metadata
```

> **Note:** `workflow_type` body field is supported as a deprecated fallback (planned removal in v3.1.0). Always use `?entrypoint=` for new code.

**Response:**
```json
{
  "data": {
    "workflow_id": "my-connector-abc123",
    "run_id": "run-xyz"
  },
  "success": true
}
```

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
    "start_time": "2025-01-01T10:00:00Z"
  }
}
```

`status` values: `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `TERMINATED`, `TIMED_OUT`.

---

### `GET /workflows/v1/result/{workflow_id}`

Fetch the most recent run result for a workflow.

**Response:**
```json
{
  "data": {
    "status": "COMPLETED",
    "output": { "record_count": 1234 }
  }
}
```

---

## Configuration Endpoints

### `GET /workflows/v1/config/{config_id}`

Retrieve a named configuration object from the state store.

### `POST /workflows/v1/config/{config_id}`

Store a named configuration object.

**Request body:** Any JSON object.

---

### `GET /workflows/v1/configmap/{config_map_id}`

Retrieve a generated configmap JSON (from `app/generated/{id}.json`).

### `GET /workflows/v1/configmaps`

List all available configmap IDs.

---

### `GET /workflows/v1/manifest`

Return the Automation Engine DAG manifest (from `app/generated/manifest.json`).

**Response:** `AppManifest` JSON — see [Multi-App Coordination](../guides/multi-app-coordination.md).

---

## File Endpoints

### `POST /workflows/v1/file`

Upload a file to be used as workflow input (e.g. CSV for file-based connectors).

**Request body:** `multipart/form-data` with a `file` field.

**Response:**
```json
{
  "data": { "file_reference": "file_refs/uuid/upload.csv" },
  "success": true
}
```

---

## Event Endpoints

### `GET /dapr/subscribe`

Returns the Dapr pub/sub subscription configuration. Called by the Dapr sidecar on startup.

### `POST /events/v1/event/{event_id}`

Receive a Dapr cloud event. Routes to the appropriate `@on_event` handler.

### `POST /events/v1/drop`

Acknowledge and discard a cloud event without processing it.

---

## Development Endpoints

### `POST /workflows/v1/dev/local-vault`

Provision credentials into the local in-memory vault for local development. Used by `run_dev_combined()`.

**Request body:** Raw credential dict (same fields as the `credentials` array, but as a flat dict).

**Response:**
```json
{ "credential_guid": "dev-abc123" }
```

This endpoint is only available when running in `combined` mode locally. It should never be exposed in production.

---

## Health and Observability

### `GET /health` · `GET /server/health`

Handler liveness check. Returns `200 OK` when the handler is ready.

```json
{ "status": "healthy" }
```

### `GET /ready` · `GET /server/ready`

Handler readiness check. Returns `200` once the Temporal client is connected.

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
