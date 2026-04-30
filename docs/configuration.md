# Configuration Reference

The Application SDK reads configuration from environment variables at startup. Variables are grouped below by subsystem. The canonical source of truth is [`application_sdk/constants.py`](../application_sdk/constants.py) (import-time constants) and [`application_sdk/main.py`](../application_sdk/main.py) (`AppConfig` runtime values).

Set variables in your shell environment, a `.env` file at the project root, or Docker `ENV` / Kubernetes `ConfigMap` / `Secret` resources. See `.env.example` at the repo root for a ready-to-copy template.

---

## Application

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_APP_MODULE` | _(required)_ | App class to load: `module.path:ClassName` (e.g. `app.app:MyExtractor`). Startup fails without it. Set in your `Dockerfile` as `ENV ATLAN_APP_MODULE=…` or pass via `--app` CLI flag. |
| `ATLAN_APP_MODE` | `combined` | Run mode: `worker`, `handler`, or `combined`. Determines which subsystems start; override via `--mode` CLI flag. |
| `ATLAN_APPLICATION_NAME` | `default` | Application name. Used in object-store paths, logging, and workflow identification. |
| `ATLAN_DEPLOYMENT_NAME` | `local` | Deployment name. Distinguishes dev / staging / prod deployments of the same app. |
| `ATLAN_TENANT_ID` | `default` | Tenant identifier for multi-tenant deployments. |
| `ATLAN_DOMAIN_NAME` | `atlan.com` | Tenant domain name. |
| `ATLAN_TEMPORARY_PATH` | `./local/tmp/` | Path for intermediate files during processing. |
| `ATLAN_CLEANUP_BASE_PATHS` | _(empty)_ | Comma-separated object-store prefixes cleaned up by `cleanup_files()`. Defaults to the workflow-scoped run path when unset. |
| `ATLAN_CONTRACT_GENERATED_DIR` | `app/generated` | Directory for generated contract JSON (configmaps, manifest). In Docker (`WORKDIR=/app`) this resolves to `/app/app/generated`. |
| `ATLAN_FRONTEND_ASSETS_PATH` | `app/generated/frontend/static` | Path to static frontend assets served by the handler. |

### App Vitals (release metadata)

Injected by the Local Marketplace into the Helm release at deploy time. Leave empty for local development.

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_APPLICATION_VERSION` | _(empty)_ | Semantic version of the app release (e.g. `1.2.3`). |
| `ATLAN_RELEASE_ID` | _(empty)_ | Release UUID from Global Marketplace. |
| `ATLAN_RELEASE_CHANNEL` | _(empty)_ | Release channel (`all`, `beta`, `staging`, `specific`). |
| `ATLAN_SDK_VERSION` | _(empty)_ | SDK version used to build this app image. |
| `ATLAN_APP_TYPE` | _(empty)_ | App type from Global Marketplace (e.g. `connector`, `system`). |
| `ATLAN_PUBLISHED_AT` | _(empty)_ | Release publication timestamp (ISO 8601). |
| `ATLAN_ENABLE_APP_VITALS` | `true` | Enable App Vitals interceptor for automatic lifecycle metrics. |

---

## Temporal / Workflow

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_TEMPORAL_HOST` | `localhost:7233` | Temporal server address (`host:port`). **v2-compat fallback:** if unset, the SDK constructs the address from `ATLAN_WORKFLOW_HOST` + `ATLAN_WORKFLOW_PORT` (deprecated; remove when all deployments set `ATLAN_TEMPORAL_HOST`). |
| `ATLAN_TEMPORAL_NAMESPACE` | `default` | Temporal namespace. **v2-compat fallback:** `ATLAN_WORKFLOW_NAMESPACE`. |
| `ATLAN_TASK_QUEUE` | _(derived)_ | Temporal task queue name. Defaults to `{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}` when unset. |
| `ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS` | `0.0.0.0:9464` | Bind address for the Temporal SDK Prometheus endpoint (~40 built-in metrics). See [Monitoring](concepts/monitoring.md). |

### Worker Versioning

Used by the Temporal Worker Deployment controller (TWD). Leave empty unless your cluster uses versioned deployments.

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_APP_BUILD_ID` | _(empty)_ | Build ID for worker versioning. Fallback: `TEMPORAL_BUILD_ID`. |
| `ATLAN_APP_DEPLOYMENT_NAME` | _(empty)_ | Worker Deployment name (`<namespace>/<twd-name>`). Fallback: `TEMPORAL_DEPLOYMENT_NAME`. |

### TLS

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_TEMPORAL_TLS_ENABLED` | `false` | Enable mTLS for the Temporal connection. |
| `ATLAN_TEMPORAL_TLS_CA_CERT_PATH` | _(empty)_ | Path to the CA certificate file. |
| `ATLAN_TEMPORAL_TLS_CLIENT_CERT_PATH` | _(empty)_ | Path to the client certificate file. |
| `ATLAN_TEMPORAL_TLS_CLIENT_KEY_PATH` | _(empty)_ | Path to the client private key file. |
| `ATLAN_TEMPORAL_TLS_DOMAIN` | _(empty)_ | TLS server name override. |

---

## HTTP Handler

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_HANDLER_HOST` | `0.0.0.0` | Bind address for the FastAPI handler. Fallback: `ATLAN_APP_HTTP_HOST`. |
| `ATLAN_HANDLER_PORT` | `8000` | HTTP port for the handler. Fallback: `ATLAN_APP_HTTP_PORT`. |
| `ATLAN_HEALTH_PORT` | `8081` | Port for the worker health endpoint. |
| `ATLAN_HANDLER_MODULE` | _(empty)_ | Handler class to load (`module:ClassName`). Auto-discovered from the app module when unset. |

---

## Authentication (Temporal)

Set these when `ATLAN_AUTH_ENABLED=true` to authenticate the worker and handler against a secured Temporal cluster.

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_AUTH_ENABLED` | `false` | Enable OAuth2 authentication for Temporal. |
| `ATLAN_AUTH_TOKEN_URL` | _(empty)_ | OAuth2 token endpoint. **v2-compat fallback:** `ATLAN_AUTH_URL`. |
| `ATLAN_AUTH_BASE_URL` | _(empty)_ | OAuth2 authorization base URL. **v2-compat fallback:** `ATLAN_AUTH_URL`. |
| `ATLAN_AUTH_CLIENT_ID` | _(empty)_ | OAuth2 client ID (direct env var). |
| `ATLAN_AUTH_CLIENT_SECRET` | _(empty)_ | OAuth2 client secret (direct env var). |
| `ATLAN_AUTH_SCOPES` | _(empty)_ | Space-separated OAuth2 scopes. |
| `ATLAN_DEPLOYMENT_SECRET_PATH` | `ATLAN_DEPLOYMENT_SECRETS` | Key name in the deployment secret store that holds the auth credentials. |
| `ATLAN_AUTH_CLIENT_ID_KEY` | `ATLAN_AUTH_CLIENT_ID` | Key name for the client ID within the deployment secret. |
| `ATLAN_AUTH_CLIENT_SECRET_KEY` | `ATLAN_AUTH_CLIENT_SECRET` | Key name for the client secret within the deployment secret. |

---

## Dapr Component Names

Dapr component names are read at module-import time (not at runtime) because the observability stack initialises before `AppConfig` exists.

| Variable | Default | Description |
|----------|---------|-------------|
| `STATE_STORE_NAME` | `statestore` | Dapr state store component name. |
| `SECRET_STORE_NAME` | `secretstore` | Dapr secret store component name. |
| `DEPLOYMENT_OBJECT_STORE_NAME` | `objectstore` | Dapr object store for workflow outputs and artifacts. |
| `UPSTREAM_OBJECT_STORE_NAME` | `objectstore` | Dapr object store for uploading data to the Atlan platform. |
| `EVENT_STORE_NAME` | `eventstore` | Dapr pub/sub component name. |
| `DEPLOYMENT_SECRET_STORE_NAME` | `deployment-secret-store` | Dapr secret store holding deployment-scoped secrets (auth credentials, etc.). |
| `DAPR_MAX_GRPC_MESSAGE_LENGTH` | `104857600` (100 MB) | Maximum gRPC message size in bytes for Dapr client calls. Increase for apps that move large payloads through Dapr state or bindings. |
| `ENABLE_ATLAN_UPLOAD` | `false` | Enable uploading processed artifacts to the Atlan platform object store. |

---

## Redis (Capacity Lock)

Used by `RedisCapacityPool` for distributed slot locking. Leave empty if you use `LocalCapacityPool` (default for local development when no Redis is configured).

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_HOST` | _(empty)_ | Redis host for direct connection. Leave empty to use Sentinel. |
| `REDIS_PORT` | _(empty)_ | Redis port for direct connection. |
| `REDIS_PASSWORD` | _(empty)_ | Redis password. |
| `REDIS_SENTINEL_SERVICE_NAME` | `mymaster` | Redis Sentinel service name. |
| `REDIS_SENTINEL_HOSTS` | _(empty)_ | Comma-separated `host:port` pairs for Redis Sentinel. |
| `IS_LOCKING_DISABLED` | `true` | Disable distributed locking (safe default for local development). Set to `false` in production when using Redis. |
| `LOCK_RETRY_INTERVAL_SECONDS` | `60` | Retry interval for lock acquisition attempts. |

---

## MCP

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_MCP` | `false` | Start an MCP server alongside the handler. Requires the `mcp` extra: `uv add "atlan-application-sdk[mcp]"`. |

---

## SQL Client

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_SQL_USE_SERVER_SIDE_CURSOR` | `true` | Use server-side cursors for SQL queries. Reduces memory for large result sets by streaming data row-by-row. |

---

## Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_MAX_CONCURRENT_STORAGE_TRANSFERS` | `4` | Maximum concurrent object-store uploads/downloads. |
| `SSL_CERT_DIR` | _(empty)_ | Directory of custom CA certificates (`.pem`, `.crt`, `.cer`, `.ca-bundle`). Used by `httpx` and `aiohttp` clients when set. |

---

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). **Fallback:** `LOG_LEVEL`. |
| `ATLAN_LOG_BATCH_SIZE` | `100` | Records buffered before flushing to the parquet sink. |
| `ATLAN_LOG_FLUSH_INTERVAL_SECONDS` | `10` | Seconds between parquet sink flushes. |
| `ATLAN_LOG_RETENTION_DAYS` | `30` | Days to retain parquet log files before cleanup. |
| `ATLAN_LOG_CLEANUP_ENABLED` | `false` | Enable automatic cleanup of old log files. |
| `ATLAN_LOG_FILE_NAME` | `log.parquet` | Parquet log file name. |

---

## Metrics

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_ENABLE_OTLP_METRICS` | `false` | Export metrics via OTLP. |
| `ATLAN_METRICS_BATCH_SIZE` | `100` | Records buffered before flushing to the parquet sink. |
| `ATLAN_METRICS_FLUSH_INTERVAL_SECONDS` | `10` | Seconds between parquet sink flushes. |
| `ATLAN_METRICS_RETENTION_DAYS` | `30` | Days to retain parquet metric files. |
| `ATLAN_METRICS_CLEANUP_ENABLED` | `false` | Enable automatic cleanup of old metric files. |
| `ATLAN_ENABLE_PROMETHEUS_METRICS` | `true` | Expose a Prometheus `/metrics` endpoint on the handler at port `ATLAN_HANDLER_PORT`. See [Monitoring](concepts/monitoring.md). |

---

## Traces

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_ENABLE_OTLP_TRACES` | `false` | Export traces via OTLP. |
| `ATLAN_TRACES_BATCH_SIZE` | `100` | Records buffered before flushing to the parquet sink. |
| `ATLAN_TRACES_FLUSH_INTERVAL_SECONDS` | `5` | Seconds between parquet sink flushes. |
| `ATLAN_TRACES_RETENTION_DAYS` | `30` | Days to retain parquet trace files. |
| `ATLAN_TRACES_CLEANUP_ENABLED` | `true` | Enable automatic cleanup of old trace files (enabled by default to prevent disk overflow). |

---

## OpenTelemetry

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_SERVICE_NAME` | _(derived from app module)_ | Service name in telemetry data. Override with `ATLAN_SERVICE_NAME` (takes priority). |
| `OTEL_SERVICE_VERSION` | _(SDK version)_ | Service version in telemetry data. |
| `OTEL_RESOURCE_ATTRIBUTES` | _(empty)_ | Additional OTel resource attributes (key=value pairs). |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP collector endpoint. Set to the node IP collector in Kubernetes: `$(K8S_NODE_IP):4317`. |
| `ENABLE_OTLP_LOGS` | `false` | Export logs via OTLP to `OTEL_EXPORTER_OTLP_ENDPOINT`. |
| `OTEL_WORKFLOW_LOGS_ENDPOINT` | _(empty)_ | Secondary OTLP endpoint for workflow logs (for dual export to a tenant-level collector). |
| `ENABLE_OTLP_WORKFLOW_LOGS` | `false` | Export workflow logs to `OTEL_WORKFLOW_LOGS_ENDPOINT`. |
| `OTEL_WF_NODE_NAME` | _(empty)_ | Kubernetes node name for workflow telemetry. |
| `OTEL_EXPORTER_TIMEOUT_SECONDS` | `30` | Timeout for OTLP export operations. |
| `OTEL_BATCH_DELAY_MS` | `5000` | Delay between batch exports (milliseconds). |
| `OTEL_BATCH_SIZE` | `512` | Maximum export batch size. |
| `OTEL_QUEUE_SIZE` | `2048` | Maximum export queue size. |
| `ATLAN_ENABLE_OBSERVABILITY_STORE_SINK` | `true` | Write observability data to the object store sink. **Fallback:** `ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK`. |
| `ATLAN_BASE_URL` | _(empty)_ | Atlan instance base URL. Used by the events interceptor. |

---

## Segment

Segment events are automatically enabled when `ATLAN_SEGMENT_WRITE_KEY` is set.

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_SEGMENT_WRITE_KEY` | _(empty)_ | Segment write key. Leave empty to disable Segment tracking. |
| `ATLAN_SEGMENT_API_URL` | `https://api.segment.io/v1/batch` | Segment batch API URL. |
| `ATLAN_SEGMENT_BATCH_SIZE` | `100` | Maximum events per batch. |
| `ATLAN_SEGMENT_BATCH_TIMEOUT_SECONDS` | `10.0` | Maximum seconds to wait before flushing a partial batch. |

---

## AWS

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_SESSION_NAME` | `temp-session` | AWS session name for temporary credentials when assuming IAM roles. |

---

## Path Templates

These are code-level constants (not environment variables). Documented here for reference when interpreting object-store paths.

| Constant | Pattern |
|----------|---------|
| `WORKFLOW_OUTPUT_PATH_TEMPLATE` | `artifacts/apps/{application_name}/workflows/{workflow_id}/{run_id}` |
| `STATE_STORE_PATH_TEMPLATE` | `persistent-artifacts/apps/{application_name}/{state_type}/{id}/config.json` |
| `OBSERVABILITY_DIR` | `artifacts/apps/{application_name}/{deployment_name}/observability` |

---

## Common Patterns

### Local development

Most defaults work out of the box for local development with `uv run poe start-deps` (starts Dapr + Temporal). The key variables to override:

```bash
ATLAN_APP_MODULE=app.app:MyExtractor
ATLAN_APPLICATION_NAME=my-extractor
ATLAN_LOG_LEVEL=DEBUG
```

### Production deployment

```bash
ATLAN_APP_MODULE=app.app:MyExtractor          # required
ATLAN_TEMPORAL_HOST=temporal.internal:7233    # production Temporal cluster
ATLAN_TEMPORAL_NAMESPACE=my-namespace
ATLAN_AUTH_ENABLED=true
ATLAN_AUTH_TOKEN_URL=https://auth.internal/oauth2/token
ATLAN_AUTH_CLIENT_ID=…                        # or use deployment secret store
ATLAN_AUTH_CLIENT_SECRET=…
ENABLE_ATLAN_UPLOAD=true
ATLAN_ENABLE_PROMETHEUS_METRICS=true          # scrape at :8000/metrics
ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS=0.0.0.0:9464  # scrape at :9464/metrics
```

### Performance tuning

```bash
ATLAN_MAX_CONCURRENT_STORAGE_TRANSFERS=8      # higher on fast network
DAPR_MAX_GRPC_MESSAGE_LENGTH=209715200        # 200 MB for large payloads
ATLAN_LOG_BATCH_SIZE=500                      # fewer flushes under load
```
