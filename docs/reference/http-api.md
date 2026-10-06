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

**Check tiers (optional).** Add `"tiers"` to the body to run only some of the checks: `["preflight"]`, `["warmup"]`, or both. A `preflight` check runs with no preparation; a `warmup` check needs the source's compute and runs only once the app's `Handler.warmup` reports `ready`. A check with no tier is `preflight`, and a `preflight` check carries no `tier` key in the response. A body with no `tiers` runs every check and never calls `warmup`, so its response is exactly what it was before tiers existed. See [Check tiers and warmup](../concepts/handlers.md#check-tiers-and-warmup) for which checks belong in which tier.

| `tiers` | Calls `warmup` | Runs | `preflight.status` |
| --- | --- | --- | --- |
| absent | no | every check | the handler's verdict |
| `["preflight"]` | no | only `preflight` checks | `pending` when the app has a warmup and the handler's verdict is not `not_ready`; otherwise the handler's verdict |
| includes `"warmup"` | once, before any check | every requested tier when the probe answers `ready`; otherwise every requested tier except `warmup` (nothing, for `["warmup"]`) | the handler's verdict on `ready`; `not_ready` on `unavailable`; otherwise `pending` unless the handler said `not_ready` |

An app *has a warmup* when its handler overrides `Handler.warmup`, or when the entry point has a `warmup` module hook. `not_ready` always wins over `pending`. A `pending` verdict means the checks that ran passed, or failed only advisorily, but the `warmup` checks have not run. `preflight.warmup` explains it: the probe's `WarmupObservation` (`state` and `source_state`, plus `queued_queries` and `next_poll_seconds` when set), or `null` for a `["preflight"]` request, which never probes. `preflight.warmup` is present on every `pending` verdict and on a `not_ready` verdict from a request that probed; otherwise it is absent. The envelope `success` is `true` on a `pending` verdict even when no check ran.

The probe is bounded by the app's `preflight_warmup_probe_timeout_seconds` (default 10s). A probe still running at that point reads as `warming`. An `unavailable` probe turns the verdict into `not_ready` with the message "The source reported its compute as unavailable (…)".

`tiers: ["preflight", "warmup"]` against a source that is still resuming:

```json
{
  "success": true,
  "data": {
    "reachable": { "success": true, "message": "", "successMessage": "", "failureMessage": "" }
  },
  "message": "Preflight check pending",
  "preflight": {
    "status": "pending",
    "message": "",
    "total_duration_ms": 0.0,
    "checks": [{ "name": "reachable", "passed": true, "message": "" }],
    "warmup": { "state": "queued", "source_state": "RESUMING", "queued_queries": 3 }
  }
}
```

`tiers: ["warmup"]` once `/warmup` reports `ready`. A `warmup` check carries its `tier`:

```json
{
  "success": true,
  "data": {
    "catalogScan": { "success": true, "message": "", "successMessage": "", "failureMessage": "" }
  },
  "message": "Preflight check ready",
  "preflight": {
    "status": "ready",
    "message": "",
    "total_duration_ms": 0.0,
    "checks": [{ "name": "catalogScan", "passed": true, "tier": "warmup", "message": "" }]
  }
}
```

**Post-call tier check.** The SDK checks every returned check's tier against the tiers the handler was asked to run. A check outside them means the handler ran something it was told not to, such as a `warmup` probe against compute that is not ready, so its verdict is not one about the requested tiers. The answer is **HTTP 500** with the unverified verdict every handler failure uses: `not_ready` and one failed `preflightVerdict` row carrying a typed `INTERNAL` error whose message names the checks ("Preflight handler returned checks outside the requested tiers: catalogScan"). Rows are never silently dropped.

An unknown tier, or an empty `tiers` list, is a 422.

---

### `POST /workflows/v1/warmup`

Probe the source's compute once, the probe that `warmup`-tier checks wait on. Stateless and idempotent: each call both pushes the warmup forward (for example, a probe query that resumes a suspended warehouse) and reports where it is, and the SDK keeps no warmup state between calls. Optional: an app that does not override `Handler.warmup` answers `ready`.

**Request body:** Same as `/check`, so the UI sends one payload to both routes. The route does not act on `tiers`.

**Response:** the handler's `WarmupObservation` under `data`, plus `ceiling_seconds`, the app's `preflight_warmup_ceiling_seconds` (default 600), so the UI has a stop condition of its own. The envelope message is `Warmup <state>`:

```json
{
  "success": true,
  "data": { "state": "queued", "source_state": "RESUMING", "queued_queries": 3, "ceiling_seconds": 600 },
  "message": "Warmup queued"
}
```

| `data.state` | `success` | Meaning |
| --- | --- | --- |
| `cold` / `warming` / `queued` | `true` | not ready yet; call again |
| `ready` | `true` | `warmup`-tier checks can run |
| `unavailable` | `false` | the source will not get ready on its own |

Every observation answers HTTP 200. `source_state` is the source's own label for display (`""` when the handler gave none), `queued_queries` the number of statements waiting for a slot, and `next_poll_seconds` the source's suggestion for when to call again; the last two are omitted when the handler did not set them. The probe is bounded by the app's `preflight_warmup_probe_timeout_seconds`, and a probe still running at that point answers `warming`, so no request is held longer than one probe timeout. A typed `AppError` raised by the handler maps to its HTTP status (an `AuthError` is 401) with the leaf's message as `detail`. Any other raise is a 500 with `"detail": "Internal server error"`.

Delegates to `Handler.warmup(WarmupInput)`, or to an entry point's `warmup` module hook when one exists.

**The UI sequence.** `POST /check` with `"tiers": ["preflight"]` answers the cheap checks without starting compute. `POST /warmup` starts the warmup and reports it; call it again until `data.state` is `ready`, waiting `next_poll_seconds` when it is set, and stop at `ceiling_seconds`. When it reports `ready`, `POST /check` with `"tiers": ["warmup"]`. On `unavailable`, show that the source's compute is unavailable instead.

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

**Query param:** `?entrypoint=<name>` (optional). When set, it selects a specific `@entrypoint`-decorated method. When omitted, the app's **default** entry point is resolved: the single `run()`/`@entrypoint` when there is only one, otherwise the one marked `default=True`, otherwise the alphabetically first. Only an app with entry points and no resolvable default returns HTTP 400 (`"entrypoint is required for this app."`).

> **Pass it explicitly on a multi-entrypoint app.** Omitting it does not fail — it silently starts whichever entry point resolves as default, so a caller (or a test) meaning to start the miner can start the crawler instead and see a success response. See [Entry points](../concepts/entry-points.md#default-entrypoint-resolution) for the full resolution table.

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

> **Note:** the `workflow_type` body field is supported as a deprecated fallback (removal in v4.0 — see the `DeprecationWarning` raised by the route). Always use `?entrypoint=` for new code.

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

**Error responses:**

- `400 {"detail": "Invalid entrypoint."}` — `?entrypoint=<name>` does not match any registered entry point.
- `400 {"detail": "entrypoint is required for this app."}` — multi-entry-point app called without `?entrypoint=`.
- `503 {"detail": "Workflow execution not configured. Set ATLAN_TEMPORAL_HOST."}` — Temporal is not configured (missing `ATLAN_TEMPORAL_HOST`).

---

### `POST /workflows/v1/stop/{workflow_id}/{run_id}`

Request graceful termination of a running workflow.

**Path params:** `workflow_id`, `run_id` (supports slashes — use URL encoding).

**Response:** `{ "success": true, "message": "Workflow terminated successfully" }`

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

**Error responses:**

- `400 {"detail": "Invalid entrypoint name"}` — `?entrypoint=` value fails the `^[a-zA-Z][a-zA-Z0-9_-]*$` validation regex.
- `404 {"detail": "No manifest found for entrypoint '<name>'"}` — no `app/generated/<name>/manifest.json` exists.
- `404 {"detail": "No manifest available"}` — no `?entrypoint=` provided and `app/generated/manifest.json` does not exist.
- `413 {"detail": "fe_inputs exceeds the 8192-byte limit"}` — decoded `?fe_inputs=` is over the query-string cap. Use `POST` (below) instead of trimming the payload.

---

### `POST /workflows/v1/manifest`

Same manifest, with the inputs in the request body instead of the query string. Prefer this whenever you send `fe_inputs`.

> **Legacy alias:** An unversioned `POST /manifest` is also registered (`include_in_schema=false`), so a caller can migrate method and path independently.

**Body** (all fields optional; `{}`, `null` and an absent body are all equivalent to a bare `GET`):

```json
{
  "entrypoint": "asset-export-advanced",
  "fe_inputs": { "attribute-selector": "table:rowCount,column:dataType" },
  "user_id": "<keycloak-guid>"
}
```

`user_id` is accepted and ignored, so callers that already send it (as a GET query param, where it is likewise ignored) keep working.

`fe_inputs` must be a JSON **object**, not a JSON string — a caller porting the GET query parameter must pass the decoded object rather than the encoded string that rode in the URL.

**Why this exists:** on `GET`, `fe_inputs` is bounded by the HTTP request line. Over 8 KB decoded the SDK returns `413` before `compute_manifest` runs, and past ~64 KB on the wire the URL is silently truncated by the parser — the app then answers `200` on a corrupt payload. A near-"select-all" asset-export-advanced submission decodes to ~11 KB and hit exactly this (CSA-539). The body has neither limit, so **no size cap applies to `POST`**.

**Response:** identical to `GET` — the two share one code path.

**Error responses:**

- `400 {"detail": "Request body is not valid JSON: ..."}`
- `400 {"detail": "Request body must be a JSON object"}` — body was a list, string, or number.
- `400 {"detail": "entrypoint must be a string"}`
- `400 {"detail": "fe_inputs must be a JSON object"}`
- `400` / `404` — as for `GET` above.

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

Receive a Dapr cloud event. Dispatches the event by starting a Temporal workflow via the app's workflow client (`client.start_workflow`), using the event payload as input.

### `POST /events/v1/drop`

Returns a Dapr `DROP` status, instructing the sidecar to drop the event without retry or dead-lettering.

---

## Development Endpoints

### `POST /workflows/v1/dev/local-vault`

Provision credentials into the local in-memory vault for local development. Used by `run_dev_combined()`.

**Request body:** Raw credential dict (same fields as the `credentials` array, but as a flat dict).

**Response:**
```json
{
  "data": { "credential_guid": "dev-abc123" },
  "success": true,
  "message": "Credentials provisioned successfully"
}
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

Prometheus metrics endpoint. Always exposed (no env-var gate). The
response merges all in-process metric sources:

- Custom metrics from `record_metric()` and direct OTel meter use
- HTTP server instrumentation (FastAPIInstrumentor, stable OTel HTTP
  semantic conventions: `http.request.method`, `http.route`, etc.)
- Temporal SDK Rust-core families when enabled and reachable (proxied
  in-process from `127.0.0.1:9464` in combined mode)
- `prometheus_client` defaults (`process_*`, `python_*`)

See [Monitoring](../concepts/monitoring.md) for the full architecture
and the role of `ATLAN_ENABLE_TEMPORAL_CORE_METRICS` (which gates only
the loopback Rust-core endpoint binding, not this route).

### `GET /`

Serves the custom frontend `index.html` when present at `ATLAN_FRONTEND_ASSETS_PATH`. Returns a minimal `text/html` 404 page (`<html><body><h1>UI not available</h1></body></html>`) when no frontend bundle is found.

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
