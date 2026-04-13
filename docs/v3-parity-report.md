# V3 Parity Report: `main` vs `refactor-v3`

Generated: 2026-04-07

## Methodology

Each user-facing endpoint and worker-side flow was traced end-to-end through
both branches, following the full request lifecycle: HTTP entry → body parsing →
credential handling → business logic → Temporal dispatch → state persistence →
response construction.

---

## 1. POST /workflows/v1/start — Workflow Start

### 1.1 Credential Extraction → Secret Store (CRITICAL)

| | main | v3 |
|---|---|---|
| **Flow** | Extracts `credentials` from body → `SecretStore.save_secret()` → replaces with `credential_guid` | Body goes straight to `input_type(**body)` — no extraction, no secret store save |
| **What reaches Temporal** | Only `workflow_id` + correlation fields (credentials stripped) | Entire `input_data` object (credentials could be embedded) |
| **Security** | Credentials never in Temporal history | If credentials somehow pass through, they persist in Temporal DB |

**Impact**: If the caller (Heracles/AE) sends raw `credentials` in the body,
v3 will either reject it (Pydantic validation error — `ExtractionInput` has no
`credentials` field) or, if `allow_unbounded_fields=True`, pass raw secrets
through Temporal.

### 1.2 Credential Format Normalization

`_normalize_credentials()` exists in v3 but is only called on `/auth`, `/check`,
`/metadata` — **not on `/start`**. Inconsistent across endpoints.

### 1.3 cron_schedule and execution_timeout (MISSING)

| | main | v3 |
|---|---|---|
| `cron_schedule` | `workflow_args.get("cron_schedule", "")` passed to Temporal | Not passed |
| `execution_timeout` | `WORKFLOW_MAX_TIMEOUT_HOURS` constant | Not passed — relies on server default |

**Impact**: Scheduled/recurring workflows and timeout enforcement are non-functional.

### 1.4 State Store Save Differences

| | main | v3 |
|---|---|---|
| **Key format** | `StateType.WORKFLOWS` with `id=workflow_id` | `f"workflows/{handle.id}"` (flat string path) |
| **Content** | Full `workflow_args` (minus credentials, plus `credential_guid`, `application_name`) | Body minus `credentials` key, plus `workflow_id` |
| **Error handling** | Integral to flow (failure raises) | Non-fatal (warning logged, continues) |

### 1.5 Workflow ID Generation

| | main | v3 |
|---|---|---|
| **Fallback** | `argo_workflow_name` or `uuid4()` | `{app_name}-{config_hash}-{random_hex}` |
| **`application_name`** | Injected into workflow_args | Not injected |

---

## 2. POST /workflows/v1/auth — Auth Test

### 2.1 Handler Initialization (BREAKING)

| | main | v3 |
|---|---|---|
| **Flow** | `handler.load(credentials)` → `handler.test_auth()` | `handler._context = HandlerContext(credentials)` → `handler.test_auth(input)` |
| **Client init** | Automatic via `handler.load()` which calls `client.load()` | Manual — handler must extract from `self.context` and call client methods |
| **Signature** | `test_auth(self) -> bool` | `test_auth(self, input: AuthInput) -> AuthOutput` |

**Impact**: All v2 handlers must be rewritten. No backward-compatible shim.

### 2.2 Response Format (BREAKING)

| | main | v3 |
|---|---|---|
| **Success** | `{"success": true, "message": "..."}` | `{"success": true, "message": "...", "data": {"status": "success", ...}}` |
| **Status codes** | Always 200, error in body | `HandlerError` → specific HTTP status (401, 403, 500) |

### 2.3 Metrics

main records `auth_requests_total` and `auth_duration_seconds`. v3 has no
built-in endpoint metrics.

---

## 3. POST /workflows/v1/check — Preflight Check

Same pattern as `/auth`:
- Handler signature changed (`preflight_check()` → `preflight_check(input: PreflightInput) -> PreflightOutput`)
- No automatic `handler.load()` → manual credential access
- Response format changed

---

## 4. POST /workflows/v1/metadata — Fetch Metadata

Same pattern as `/auth`:
- Handler signature changed
- No automatic client initialization
- Response format changed

---

## 5. GET /workflows/v1/status/{workflow_id}/{run_id}

| | main | v3 |
|---|---|---|
| **Implementation** | Queries Temporal client for workflow status | Queries Temporal client similarly |
| **Response** | `WorkflowExecutionStatus` enum value | Same structure |

**Status**: Functionally equivalent.

---

## 6. POST /workflows/v1/stop/{workflow_id}/{run_id}

| | main | v3 |
|---|---|---|
| **Implementation** | `workflow_client.stop_workflow()` → cancels Temporal workflow | Similar — gets handle, calls `cancel()` |

**Status**: Functionally equivalent.

---

## 7. GET/POST /workflows/v1/config/{config_id}

| | main | v3 |
|---|---|---|
| **GET** | `StateStore.get_state_object()` | `_state_store.load()` |
| **POST** | `StateStore.save_state_object()` | `_state_store.save()` |

**Status**: Functionally equivalent (different API surface, same behavior).

---

## 8. POST /workflows/v1/file — File Upload

Both present. v3 uses `_storage.upload()` instead of `ObjectStore.upload()`.
Functionally equivalent.

---

## 9. GET /workflows/v1/configmap(s)

Both present. v3 reads from `_state_store` instead of `StateStore` class methods.
Functionally equivalent.

---

## 10. GET /manifest

Both present. v3 adds unversioned `/manifest` alias alongside
`/workflows/v1/manifest`. v3 reads from `CONTRACT_GENERATED_DIR` instead of
hardcoded `contract/generated/`.

---

## 11. Event-Driven Endpoints

### 11.1 GET /dapr/subscribe

Both present. v3 constructs bulk config dict manually instead of using
`model_dump(by_alias=True)`. Potential serialization mismatch if field names
differ.

### 11.2 POST /events/v1/event/{event_id}

| | main | v3 |
|---|---|---|
| **Input validation** | Cloud Events Pydantic model (`EventWorkflowRequest`) | Raw JSON, minimal validation |
| **Trigger lookup** | Closure-captured `workflow_class` | Lookup by `event_id`, 404 if missing |
| **Credential handling** | Same as `/start` (goes through `TemporalWorkflowClient.start_workflow`) | No credential extraction/save — same gap as `/start` |
| **Response format** | `{"success": true, "message": "...", "data": {...}, "status": "SUCCESS"}` | `{"status": "SUCCESS", "workflow_id": "...", "run_id": "..."}` |
| **Error response** | `status: "DROP"` | `status: "RETRY"` with HTTP 500 |

**Impact**: Response shape change breaks callers. Error semantics differ (DROP vs RETRY).

### 11.3 POST /events/v1/drop

v3 drops `message` and `data` fields from response. Minor.

---

## 12. Worker-Side Execution

### 12.1 Interceptor Parity

| Interceptor | main | v3 | Status |
|---|---|---|---|
| CorrelationContext | Registered | Registered (different impl) | Partial — only `x-correlation-id` propagated, `atlan-*` fields lost |
| Event | Registered | Registered | Changed — uses binding instead of activity (silent fail possible) |
| Cleanup | Registered | **NOT registered** | **MISSING** — cleanup is opt-in via `on_complete()` |
| Lock | Registered | **NOT registered** | **MISSING** — `@needs_lock` tasks won't acquire locks |
| ActivityFailureLogging | Registered | Registered (renamed TaskFailureLogging) | OK |
| ExecutionContext | N/A | Registered (NEW) | New in v3 |

### 12.2 Lock Interceptor (CRITICAL)

`RedisLockInterceptor` code exists in v3 at
`execution/_temporal/interceptors/lock.py` but `create_worker()` does NOT
register it. Activities decorated with `@needs_lock` will silently skip lock
acquisition — data races possible.

### 12.3 Cleanup (CRITICAL)

main's `CleanupInterceptor` guarantees temp file cleanup on workflow
completion (success or failure). v3 marks it deprecated and does NOT register
it. Cleanup only happens if the app explicitly calls `cleanup_files()` in
`on_complete()`. If the app doesn't, or if the workflow fails before reaching
`on_complete()`, temp files persist indefinitely.

### 12.4 Credential Resolution on Worker

| | main | v3 |
|---|---|---|
| **Flow** | `workflow_id` → `StateStore.get_state_object()` → `credential_guid` → `SecretStore.get_credentials()` | `credential_ref` or `credential_guid` in Input → task must manually resolve via `context.resolve_credential()` |
| **Responsibility** | Framework resolves automatically in `_set_state()` | Each connector must resolve explicitly |

### 12.5 Event Publishing Reliability

| | main | v3 |
|---|---|---|
| **Mechanism** | `publish_event` activity with retry policy (3 attempts) | Direct binding call — silent failure if binding unavailable |

### 12.6 Correlation Context Propagation

| | main | v3 |
|---|---|---|
| **Fields propagated** | All `atlan-*` fields + `correlation_id` + `trace_id` | Only `x-correlation-id` |

**Impact**: Tenant ID, client ID, and other atlan-prefixed context is lost in
v3 activity calls.

---

## Summary: Critical Gaps

| # | Gap | Severity | Endpoint/Flow |
|---|-----|----------|---------------|
| 1 | No credential → secret store save in `/start` | **Critical** | /start, /events |
| 2 | Lock interceptor not registered | **Critical** | Worker |
| 3 | Cleanup interceptor not registered | **Critical** | Worker |
| 4 | `cron_schedule` / `execution_timeout` not passed | **High** | /start |
| 5 | `_normalize_credentials` not called on `/start` | **High** | /start |
| 6 | Event response format changed (DROP→RETRY, shape) | **High** | /events |
| 7 | `atlan-*` correlation fields not propagated | **Medium** | Worker |
| 8 | Event publishing silent failure | **Medium** | Worker |
| 9 | Handler signature breaking change (no shim) | **Medium** | /auth, /check, /metadata |
| 10 | Endpoint metrics removed | **Low** | /auth, /check, /metadata |
