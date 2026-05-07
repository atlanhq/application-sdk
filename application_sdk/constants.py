"""Application SDK configuration constants.

This module provides **import-time** configuration — values read from environment
variables once at module import, before ``AppConfig`` is constructed. They are used
by code that initialises at import time (observability stack, OTEL exporters,
logging.basicConfig, Dapr component names).

**When to use constants.py vs AppConfig:**

* ``constants.py`` — for values consumed at **module level** (top of file, class
  body, default parameters). These run before ``main()`` or ``run_dev_combined()``
  and cannot wait for ``AppConfig``.
* ``AppConfig`` (in ``main.py``) — for values consumed at **runtime** inside
  functions and methods. ``AppConfig`` is the single authoritative config object
  passed through the call chain.

Both read from the **same environment variables with the same defaults** so they
stay in sync. The env var is the single source of truth — it is set once by
Helm/Docker/.env before the process starts and both readers see the same value.

**Why not move everything to AppConfig?**
The observability layer (``logger_adaptor``, ``traces_adaptor``, ``metrics_adaptor``)
calls ``logging.basicConfig(level=LOG_LEVEL)`` and creates ``MeterProvider`` /
``TracerProvider`` at **module import time** — before ``main()`` parses CLI args and
constructs ``AppConfig``. Moving these to AppConfig would require making the entire
observability stack lazy-initializing (defer setup until first use), which is a large
refactor with the following risks:

- **Lost early logs**: any log emitted before ``configure()`` is called would be
  silently dropped or go to a default handler with wrong level/format. This makes
  startup failures harder to diagnose — exactly when logs matter most.
- **Thread safety**: lazy init requires double-checked locking or ``threading.Once``
  to avoid races when multiple threads (Temporal worker, health server, handler)
  trigger first-use concurrently.
- **Import-order sensitivity**: if any module happens to log during import (common
  for dependency warnings, deprecation notices), the lazy guard must handle the
  "not yet configured" state gracefully without crashing or losing the message.
- **Test isolation**: every test that touches logging/metrics/traces would need to
  reset the lazy singleton, adding fragile teardown logic across ~100 test files.

Until the observability stack is refactored, constants.py provides the import-time
values it needs. The env var is the single source of truth for both readers.

**Adding new config:**
If the consumer runs at import time → add to ``constants.py``.
If the consumer runs at runtime → add to ``AppConfig`` only.
If both → add to both with the same env var and default, and document the link.

Example:
    >>> from application_sdk.constants import APPLICATION_NAME
    >>> print(f"Running application {APPLICATION_NAME}")
"""

import os
from enum import Enum

import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

# Static Constants
LOCAL_ENVIRONMENT = "local"

# Application Constants
#: Name of the application, used for identification
APPLICATION_NAME = os.getenv("ATLAN_APPLICATION_NAME", "default")
#: Name of the deployment, used to distinguish between different deployments of the same application
DEPLOYMENT_NAME = os.getenv("ATLAN_DEPLOYMENT_NAME", LOCAL_ENVIRONMENT)
# REMOVED: APP_HOST, APP_PORT — use AppConfig.handler_host / handler_port
#: Tenant ID for multi-tenant applications
APP_TENANT_ID = os.getenv("ATLAN_TENANT_ID", "default")
# Domain Name of the tenant
DOMAIN_NAME = os.getenv("ATLAN_DOMAIN_NAME", "atlan.com")

# App Vitals / Release metadata (injected by Local Marketplace into HelmRelease).
# Naming aligned with Anuj's LM-integration PR so they merge cleanly.
#: Semantic version of the app release (e.g., "1.2.3")
APPLICATION_VERSION = os.getenv("ATLAN_APPLICATION_VERSION", "")
#: Release UUID from Global Marketplace
RELEASE_ID = os.getenv("ATLAN_RELEASE_ID", "")
#: Release channel (all, beta, staging, specific)
RELEASE_CHANNEL = os.getenv("ATLAN_RELEASE_CHANNEL", "")
#: SDK version used to build this app image
APP_SDK_VERSION = os.getenv("ATLAN_SDK_VERSION", "")
#: App type from Global Marketplace (connector, system, etc.)
APP_TYPE = os.getenv("ATLAN_APP_TYPE", "")
#: Release publication timestamp from Global Marketplace (ISO 8601)
PUBLISHED_AT = os.getenv("ATLAN_PUBLISHED_AT", "")
# REMOVED: APP_DASHBOARD_HOST, APP_DASHBOARD_PORT, SQL_SERVER_MIN_VERSION,
# SQL_QUERIES_PATH — unused internally, v2-only external consumers.

# Output Path Constants
#: Output path format for workflows.
#:
#: Example: objectstore://bucket/artifacts/apps/{application_name}/workflows/{workflow_id}/{workflow_run_id}
WORKFLOW_OUTPUT_PATH_TEMPLATE = (
    "artifacts/apps/{application_name}/workflows/{workflow_id}/{run_id}"
)

# Temporary Path (used to store intermediate files)
TEMPORARY_PATH = os.getenv("ATLAN_TEMPORARY_PATH", "./local/tmp/")

# Directory where contract-toolkit generated files (configmaps, manifest, Python types) live.
# Convention: app/generated/ inside the repo (importable as app.generated).
# In Docker (WORKDIR=/app, app code at /app/app/) this resolves to /app/app/generated.
CONTRACT_GENERATED_DIR = os.environ.get("ATLAN_CONTRACT_GENERATED_DIR", "app/generated")

# Cleanup Paths (custom paths for cleanup operations, supports multiple paths separated by comma)
# If empty, cleanup activities will default to workflow-specific paths at runtime
CLEANUP_BASE_PATHS = [
    path.strip()
    for path in os.getenv("ATLAN_CLEANUP_BASE_PATHS", "").split(",")
    if path.strip()
]

# Key used to store tracked FileReference objects in _app_state during a workflow run
TRACKED_FILE_REFS_KEY = "_tracked_file_refs"

# Object-store prefixes that must never be deleted by cleanup_storage.
# These store cross-run persistent state (connection configs, incremental markers, etc.)
PROTECTED_STORAGE_PREFIXES = ("persistent-artifacts/",)

# State Store Constants
#: Path template for state store files.
#:
#: Example: objectstore://bucket/persistent-artifacts/apps/{application_name}/{state_type}/{id}/config.json
STATE_STORE_PATH_TEMPLATE = (
    "persistent-artifacts/apps/{application_name}/{state_type}/{id}/config.json"
)

# Observability Constants
#: Directory for storing observability data
OBSERVABILITY_DIR = "artifacts/apps/{application_name}/{deployment_name}/observability"

# Temporal Prometheus Metrics
#: Bind address for the Temporal Runtime's Rust-core Prometheus endpoint.
#: Defaults to loopback (127.0.0.1) so the endpoint is not externally
#: reachable — the FastAPI ``/metrics`` endpoint proxies it on the server,
#: and the worker's ``TemporalCoreCollector`` scrapes it locally to feed the
#: Pushgateway push.
TEMPORAL_PROMETHEUS_BIND_ADDRESS = os.getenv(
    "ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS", "127.0.0.1:9464"
)
#: Per-request HTTP timeout for the in-process FastAPI ``/metrics`` proxy
#: that fetches Temporal Rust-core metrics from the loopback endpoint. Too
#: low and a GC pause / CPU throttle silently drops ~460 series per scrape;
#: too high and the outer Prometheus scrape (typical 10s budget) hits its
#: own timeout. Default 5s gives ~2.5× tail-latency headroom while staying
#: well inside vmagent's per-target budget.
TEMPORAL_CORE_METRICS_PROXY_TIMEOUT_SECONDS: float = float(
    os.getenv("ATLAN_TEMPORAL_CORE_METRICS_PROXY_TIMEOUT_SECONDS", "5.0")
)

# Prometheus Pushgateway (worker-only deployments)
#: Pushgateway URL workers push to. Empty disables push (combined-mode
#: server+worker deployments leave this unset; ``/metrics`` covers
#: everything via in-process proxy).
PROMETHEUS_PUSHGATEWAY_URL = os.getenv("ATLAN_PROMETHEUS_PUSHGATEWAY_URL", "")
#: Periodic push interval (seconds). A final push always happens on shutdown.
PROMETHEUS_PUSHGATEWAY_INTERVAL_SECONDS = float(
    os.getenv("ATLAN_PROMETHEUS_PUSHGATEWAY_INTERVAL_SECONDS", "30")
)
#: When True (default), call ``delete_from_gateway`` on graceful worker
#: shutdown so stopped pods don't leave sticky data in the Pushgateway.
#: Pushgateway has no built-in TTL, so without this every pod ever scraped
#: leaks its series until manually deleted. Opt out only if you specifically
#: need the last interval of metrics to persist after a graceful exit.
PROMETHEUS_PUSHGATEWAY_DELETE_ON_SHUTDOWN = (
    os.getenv("ATLAN_PROMETHEUS_PUSHGATEWAY_DELETE_ON_SHUTDOWN", "true").lower()
    == "true"
)
#: When True (default), sweep stale ``{job=mine, instance=other}`` groups on
#: worker startup before the first push. Covers OOM / eviction / SIGKILL
#: leaks that DELETE_ON_SHUTDOWN can't catch — the next worker tidies up
#: after its predecessor. Strictly job-scoped (never touches other apps'
#: groups) and threshold-gated (never reaps live siblings).
PROMETHEUS_PUSHGATEWAY_SWEEP_STALE_ON_START = (
    os.getenv("ATLAN_PROMETHEUS_PUSHGATEWAY_SWEEP_STALE_ON_START", "true").lower()
    == "true"
)
#: Staleness threshold for the startup sweep. Groups whose last push is more
#: recent than this are left alone — protects live siblings during Temporal
#: Worker Deployments overlap (old pod draining + new pod ramping). Default
#: 300s = 10× the default push interval, comfortably above any normal blip.
PROMETHEUS_PUSHGATEWAY_SWEEP_STALENESS_SECONDS = float(
    os.getenv("ATLAN_PROMETHEUS_PUSHGATEWAY_SWEEP_STALENESS_SECONDS", "300")
)
#: Per-request HTTP timeout for every Pushgateway call (push, DELETE,
#: sweep GET, sweep per-group DELETE). The prometheus_client default of 30s
#: is too generous for our shape: at shutdown, two back-to-back 30s hangs
#: (final push + DELETE_ON_SHUTDOWN) would exceed Kubernetes' default 30s
#: terminationGracePeriodSeconds and SIGKILL the pod before cleanup runs.
#: 10s leaves 2/3 of the push interval for actual work and ~10s of slack
#: inside the grace period for the rest of worker shutdown.
PROMETHEUS_PUSHGATEWAY_HTTP_TIMEOUT_SECONDS = float(
    os.getenv("ATLAN_PROMETHEUS_PUSHGATEWAY_HTTP_TIMEOUT_SECONDS", "10")
)
#: Sleep interval between the final push and the DELETE_ON_SHUTDOWN call
#: so Prometheus has at least one scrape opportunity to read the final
#: batch before we wipe the group. Without this delay, the DELETE happens
#: milliseconds after the push and the last push window of metrics — in
#: particular, the activity-failure increments that often trigger the
#: scale-down — never reaches Prometheus. Default 35s = one full 30s
#: scrape interval (the cluster default for the Pushgateway scrape, see
#: vmagent's rendered config) plus a 5s jitter buffer. Worker pods have a
#: 12h terminationGracePeriodSeconds, so the extra wait doesn't approach
#: the kill timeout. Set to 0 to disable.
PROMETHEUS_PUSHGATEWAY_SHUTDOWN_DELETE_DELAY_SECONDS = float(
    os.getenv("ATLAN_PROMETHEUS_PUSHGATEWAY_SHUTDOWN_DELETE_DELAY_SECONDS", "35")
)

# REMOVED: ENABLE_TEMPORAL_ACTIVITY_FAILURE_LOGGING, WORKFLOW_UI_HOST,
# WORKFLOW_UI_PORT, WORKFLOW_MAX_TIMEOUT_HOURS, WORKFLOW_HOST, WORKFLOW_PORT,
# WORKFLOW_NAMESPACE — unused or moved to AppConfig.
# REMOVED: MAX_CONCURRENT_ACTIVITIES — unused, see ExecutionSettings.max_concurrent_activities

#: Maximum concurrent object-store transfers (uploads / downloads)
MAX_CONCURRENT_STORAGE_TRANSFERS = int(
    os.getenv("ATLAN_MAX_CONCURRENT_STORAGE_TRANSFERS", "4")
)

# FileReference chunked-download configuration
#: File size threshold above which downloads use parallel range GETs (default 32 MiB)
FILE_REF_CHUNKED_THRESHOLD_BYTES = int(
    os.getenv("ATLAN_FILE_REF_CHUNKED_THRESHOLD_BYTES", str(32 * 1024 * 1024))
)
#: Size of each range-GET chunk in a chunked download (default 16 MiB)
FILE_REF_CHUNK_SIZE_BYTES = int(
    os.getenv("ATLAN_FILE_REF_CHUNK_SIZE_BYTES", str(16 * 1024 * 1024))
)
#: Maximum concurrent range-GET chunks per file (default 4)
FILE_REF_CHUNK_CONCURRENCY = int(os.getenv("ATLAN_FILE_REF_CHUNK_CONCURRENCY", "4"))

#: Build ID for worker versioning (injected by TWD controller via Kubernetes Downward API).
#: When set, workers identify themselves with this build ID so the Temporal server can
#: route tasks to the correct version during versioned deployments.
APP_BUILD_ID = os.getenv("ATLAN_APP_BUILD_ID") or os.getenv("TEMPORAL_BUILD_ID", "")

#: Worker Deployment name (injected by TWD controller).
#: Format: "<namespace>/<twd-name>". When set together with APP_BUILD_ID,
#: workers register as a Worker Deployment version instead of using legacy Build ID versioning.
APP_DEPLOYMENT_NAME = os.getenv("ATLAN_APP_DEPLOYMENT_NAME") or os.getenv(
    "TEMPORAL_DEPLOYMENT_NAME", ""
)


#: Name of the deployment secrets in the secret store
DEPLOYMENT_SECRET_PATH = os.getenv(
    "ATLAN_DEPLOYMENT_SECRET_PATH", "ATLAN_DEPLOYMENT_SECRETS"
)
#: Used by events interceptor at import time (before AppConfig exists).
#: AppConfig.auth_enabled is the runtime equivalent — this constant remains
#: because the interceptor reads it at module level.
AUTH_ENABLED = os.getenv("ATLAN_AUTH_ENABLED", "false").lower() == "true"
#: OAuth2 authentication URL for workflow services
AUTH_URL = os.getenv("ATLAN_AUTH_URL")
# REMOVED: WORKFLOW_TLS_ENABLED — v2-only consumers, v3 is a breaking release.

# Deployment Secret Store Key Names
#: Key name for OAuth2 client ID in deployment secrets (can be overridden via ATLAN_AUTH_CLIENT_ID_KEY)
WORKFLOW_AUTH_CLIENT_ID_KEY = os.getenv(
    "ATLAN_AUTH_CLIENT_ID_KEY", "ATLAN_AUTH_CLIENT_ID"
)
#: Key name for OAuth2 client secret in deployment secrets (can be overridden via ATLAN_AUTH_CLIENT_SECRET_KEY)
WORKFLOW_AUTH_CLIENT_SECRET_KEY = os.getenv(
    "ATLAN_AUTH_CLIENT_SECRET_KEY", "ATLAN_AUTH_CLIENT_SECRET"
)

# REMOVED: HEARTBEAT_TIMEOUT, START_TO_CLOSE_TIMEOUT, GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
# These were never used. See ExecutionSettings for the actual runtime values:
#   - ExecutionSettings.graceful_shutdown_timeout_seconds (TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT)
#   - @task(timeout_seconds=..., heartbeat_timeout_seconds=...) for per-task timeouts

#: Delay before initiating worker shutdown after receiving a termination signal.
#: This gives the event loop time to flush in-flight activity completions
#: (e.g. RespondActivityTaskFailed/Completed RPCs) that are already queued
#: but haven't been sent yet. Without this, a SIGTERM that arrives right
#: after an activity fails can preempt the _run_activity coroutine before
#: it reaches complete_activity_task(), leaving the SDK with a phantom
#: "in-use" task slot that blocks shutdown for the entire
#: graceful_shutdown_timeout.
SHUTDOWN_DRAIN_DELAY_SECONDS = int(os.getenv("ATLAN_SHUTDOWN_DRAIN_DELAY_SECONDS", 5))


#: Maximum re-dispatches per activity caused by worker pod eviction (SIGTERM
#: during activity execution: KEDA scale-down, VPA eviction, spot reclaim,
#: node drain, rolling deploy). These re-dispatches do not consume the
#: activity's Temporal RetryPolicy ``max_attempts`` budget, so this cap bounds
#: the worst case (e.g. a flaky liveness probe masquerading as eviction).
#: Validated to a non-negative integer; malformed values fall back to 3.
def _load_worker_eviction_max_retries() -> int:
    raw = os.getenv("ATLAN_WORKER_EVICTION_MAX_RETRIES", "3")
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(0, value)


WORKER_EVICTION_MAX_RETRIES = _load_worker_eviction_max_retries()

# SQL Client Constants
#: Whether to use server-side cursors for SQL operations
USE_SERVER_SIDE_CURSOR = bool(os.getenv("ATLAN_SQL_USE_SERVER_SIDE_CURSOR", "true"))

# DAPR Constants
#: Name of the state store component in DAPR
STATE_STORE_NAME = os.getenv("STATE_STORE_NAME", "statestore")
#: Name of the secret store component in DAPR
SECRET_STORE_NAME = os.getenv("SECRET_STORE_NAME", "secretstore")
#: Name of the deployment object store component in DAPR
DEPLOYMENT_OBJECT_STORE_NAME = os.getenv("DEPLOYMENT_OBJECT_STORE_NAME", "objectstore")
#: Name of the upstream object store component in DAPR
UPSTREAM_OBJECT_STORE_NAME = os.getenv("UPSTREAM_OBJECT_STORE_NAME", "objectstore")
#: Name of the pubsub component in DAPR
EVENT_STORE_NAME = os.getenv("EVENT_STORE_NAME", "eventstore")
#: DAPR binding operation for creating resources
DAPR_BINDING_OPERATION_CREATE = "create"
#: Version of worker start events used in the application
WORKER_START_EVENT_VERSION = "v1"

# HTTP Connection Pool Configuration (BLDX-1153).
# Prevents CLOSE_WAIT zombie socket accumulation and infinite blocking.
# keepalive_expiry=30s < nginx keepalive_timeout=75s → client retires idle
# connections before nginx sends FIN that httpcore can't detect.
# pool timeout=30s → threads raise PoolTimeout instead of blocking forever.
_HTTP_POOL_LIMITS = httpx.Limits(
    max_connections=50,
    max_keepalive_connections=10,
    keepalive_expiry=30.0,
)
_HTTP_POOL_TIMEOUT_SECONDS = 30.0

#: Whether to enable Atlan storage upload
ENABLE_ATLAN_UPLOAD = os.getenv("ENABLE_ATLAN_UPLOAD", "false").lower() == "true"
# Dapr Client Configuration
#: Maximum gRPC message length in bytes for Dapr client.
#:
#: Default: 100MB
DAPR_MAX_GRPC_MESSAGE_LENGTH = int(
    os.getenv("DAPR_MAX_GRPC_MESSAGE_LENGTH", "104857600")
)

#: Name of the deployment secret store component in DAPR
DEPLOYMENT_SECRET_STORE_NAME = os.getenv(
    "DEPLOYMENT_SECRET_STORE_NAME", "deployment-secret-store"
)

# Logger Constants
#: Log level for the application (DEBUG, INFO, WARNING, ERROR, CRITICAL).
#: Used by logger_adaptor.py at module level (logging.basicConfig runs at
#: import time, before AppConfig exists). AppConfig.log_level is the runtime
#: equivalent for code that has access to the config instance.
LOG_LEVEL = (os.getenv("ATLAN_LOG_LEVEL") or os.getenv("LOG_LEVEL", "INFO")).upper()
#: Service name for OpenTelemetry.
#: Used by traces_adaptor.py and metrics_adaptor.py at module level for
#: tracer/meter initialization. AppConfig.service_name is the runtime equivalent.
SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "atlan-application-sdk")
#: Service version for OpenTelemetry
SERVICE_VERSION: str = os.getenv("OTEL_SERVICE_VERSION", "")
if not SERVICE_VERSION:
    from application_sdk.version import __version__

    SERVICE_VERSION = __version__
#: Additional resource attributes for OpenTelemetry
OTEL_RESOURCE_ATTRIBUTES: str = os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")
#: Endpoint for the OpenTelemetry collector
OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
)
#: Whether to enable OpenTelemetry log export
ENABLE_OTLP_LOGS: bool = os.getenv("ENABLE_OTLP_LOGS", "false").lower() == "true"
#: Whether to enable a secondary OpenTelemetry log exporter for workflow-log
#: archival (e.g. S3 sink). When true, logs are emitted to both the primary
#: OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_WORKFLOW_LOGS_ENDPOINT.
ENABLE_OTLP_WORKFLOW_LOGS: bool = (
    os.getenv("ENABLE_OTLP_WORKFLOW_LOGS", "false").lower() == "true"
)
#: Endpoint for the secondary archival OTel collector
OTEL_WORKFLOW_LOGS_ENDPOINT: str = os.getenv("OTEL_WORKFLOW_LOGS_ENDPOINT", "")

# OTEL Constants
#: Node name for workflow telemetry
OTEL_WF_NODE_NAME = os.getenv("OTEL_WF_NODE_NAME", "")
#: Timeout for OpenTelemetry exporters in seconds
OTEL_EXPORTER_TIMEOUT_SECONDS = int(os.getenv("OTEL_EXPORTER_TIMEOUT_SECONDS", "30"))
#: Delay between batch exports in milliseconds
OTEL_BATCH_DELAY_MS = int(os.getenv("OTEL_BATCH_DELAY_MS", "5000"))
#: Maximum size of export batches
OTEL_BATCH_SIZE = int(os.getenv("OTEL_BATCH_SIZE", "512"))
#: Maximum size of the export queue
OTEL_QUEUE_SIZE = int(os.getenv("OTEL_QUEUE_SIZE", "2048"))

# AWS Constants
#: AWS Session Name
AWS_SESSION_NAME = os.getenv("AWS_SESSION_NAME", "temp-session")

# Log batching configuration
LOG_BATCH_SIZE = int(os.environ.get("ATLAN_LOG_BATCH_SIZE", 100))
LOG_FLUSH_INTERVAL_SECONDS = int(os.environ.get("ATLAN_LOG_FLUSH_INTERVAL_SECONDS", 10))

# Log Retention configuration
LOG_RETENTION_DAYS = int(os.environ.get("ATLAN_LOG_RETENTION_DAYS", 30))
LOG_CLEANUP_ENABLED = os.getenv("ATLAN_LOG_CLEANUP_ENABLED", "false").lower() == "true"

# Log Location configuration
LOG_FILE_NAME = os.environ.get("ATLAN_LOG_FILE_NAME", "log.parquet")
# REMOVED: ENABLE_HIVE_PARTITIONING — unused.

# Metrics batching / sink configuration
METRICS_BATCH_SIZE = int(os.environ.get("ATLAN_METRICS_BATCH_SIZE", 100))
METRICS_FLUSH_INTERVAL_SECONDS = int(
    os.environ.get("ATLAN_METRICS_FLUSH_INTERVAL_SECONDS", 10)
)
METRICS_RETENTION_DAYS = int(os.environ.get("ATLAN_METRICS_RETENTION_DAYS", 30))
METRICS_CLEANUP_ENABLED = (
    os.getenv("ATLAN_METRICS_CLEANUP_ENABLED", "false").lower() == "true"
)
METRICS_FILE_NAME = os.environ.get("ATLAN_METRICS_FILE_NAME", "metrics.parquet")
TRACES_FILE_NAME = os.environ.get("ATLAN_TRACES_FILE_NAME", "traces.parquet")

# Segment Configuration
#: Segment API URL for sending events. Defaults to https://api.segment.io/v1/batch
SEGMENT_API_URL = os.getenv("ATLAN_SEGMENT_API_URL", "https://api.segment.io/v1/batch")
#: Segment write key for authentication. If set, Segment metrics are automatically enabled.
SEGMENT_WRITE_KEY = os.getenv("ATLAN_SEGMENT_WRITE_KEY", "")
#: Default user ID for Segment events
SEGMENT_DEFAULT_USER_ID = "atlan.automation"
#: Maximum batch size for Segment events
SEGMENT_BATCH_SIZE = int(os.getenv("ATLAN_SEGMENT_BATCH_SIZE", "100"))
#: Maximum time to wait before sending a batch (in seconds)
SEGMENT_BATCH_TIMEOUT_SECONDS = float(
    os.getenv("ATLAN_SEGMENT_BATCH_TIMEOUT_SECONDS", "10.0")
)

# Traces Configuration
#: Whether to register Temporal's OTel TracingInterceptor and emit spans.
#: Production does not yet support traces; default is False.
ENABLE_OTLP_TRACES = os.getenv("ATLAN_ENABLE_OTLP_TRACES", "false").lower() == "true"

# Store Sink Configuration (defaults to enabled)
ENABLE_OBSERVABILITY_STORE_SINK: bool = (
    os.getenv(
        "ATLAN_ENABLE_OBSERVABILITY_STORE_SINK",
        os.getenv("ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK", "true"),
    ).lower()
    == "true"
)

# REMOVED: ATLAN_API_TOKEN_GUID, ATLAN_API_KEY, ATLAN_CLIENT_ID, ATLAN_CLIENT_SECRET — unused.
# ATLAN_BASE_URL is still used by events interceptor (deferred import).
ATLAN_BASE_URL = os.getenv("ATLAN_BASE_URL")
# Lock Configuration
LOCK_METADATA_KEY = "__lock_metadata__"

# Redis Lock Configuration
#: Redis host for direct connection (when not using Sentinel)
REDIS_HOST = os.getenv("REDIS_HOST", "")
#: Redis port for direct connection (when not using Sentinel)
REDIS_PORT = os.getenv("REDIS_PORT", "")
#: Redis password (required for authenticated Redis instances)
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
#: Redis Sentinel service name. Default: mymaster
REDIS_SENTINEL_SERVICE_NAME = os.getenv("REDIS_SENTINEL_SERVICE_NAME", "mymaster")
#: Redis Sentinel hosts as comma-separated host:port pairs
REDIS_SENTINEL_HOSTS = os.getenv("REDIS_SENTINEL_HOSTS", "")
#: Whether to enable strict locking
IS_LOCKING_DISABLED = os.getenv("IS_LOCKING_DISABLED", "true").lower() == "true"
#: Retry interval for lock acquisition
LOCK_RETRY_INTERVAL_SECONDS = int(os.getenv("LOCK_RETRY_INTERVAL_SECONDS", "60"))

# MCP Configuration
#: Flag to indicate if MCP should be enabled or not. Turning this to true will setup an MCP server along
#: with the application.
ENABLE_MCP = os.getenv("ENABLE_MCP", "false").lower() == "true"
MCP_METADATA_KEY = "__atlan_application_sdk_mcp_metadata"

#: Windows extended-length path prefix
WINDOWS_EXTENDED_PATH_PREFIX = "\\\\?\\"

# SSL Configuration
#: Custom SSL certificate directory path.
#: When set, httpx and aiohttp clients will use certificates from this directory.
SSL_CERT_DIR = os.getenv("SSL_CERT_DIR", "")


class ApplicationMode(str, Enum):
    """Application execution mode.

    Determines which components of the application are started:
    - LOCAL: Starts both the worker (daemon mode) and the server. Used for local development.
    - WORKER: Starts only the worker (non-daemon mode). Used in production for worker pods.
    - SERVER: Starts only the server. Used in production for API server pods.
    """

    LOCAL = "LOCAL"
    WORKER = "WORKER"
    SERVER = "SERVER"


APPLICATION_MODE = ApplicationMode(os.getenv("APPLICATION_MODE", "LOCAL").upper())

# =============================================================================
# Incremental Extraction Constants
# =============================================================================

#: Prefix for storing marker timestamp and current state of a connection in ObjectStore
#: Example: persistent-artifacts/apps/oracle/connection/1764230875
PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE = (
    "persistent-artifacts/apps/{application_name}/connection/{connection_id}"
)

#: Maximum number of column extraction batch activities to execute in parallel
#: Controls concurrency during incremental column extraction
MAX_CONCURRENT_COLUMN_BATCHES = 3

#: Subpath template for per-run incremental diff (under connection prefix)
#: Full path: {PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE}/{INCREMENTAL_DIFF_SUBPATH_TEMPLATE}
#: Example: persistent-artifacts/apps/oracle/connection/123456/runs/abc-def-ghi/incremental-diff
INCREMENTAL_DIFF_SUBPATH_TEMPLATE = "runs/{run_id}/incremental-diff"

#: Format for marker timestamp in incremental extraction
#: Example: 2025-12-08T10:00:00Z
MARKER_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Default incremental state for first run (when incremental_state field doesn't exist)
#: Required by coalesce function in DuckDB
INCREMENTAL_DEFAULT_STATE = "NO CHANGE"

#: Base folder for DuckDB temp files (each connection gets a unique UUID subfolder)
DUCKDB_COMMON_TEMP_FOLDER = "/tmp/incremental_duckdb"

#: Default memory limit for DuckDB (fixed for K8s pods)
DUCKDB_DEFAULT_MEMORY_LIMIT = "2GB"

SSL_CERT_DIR = os.getenv("SSL_CERT_DIR", "")
"""Custom SSL/TLS certificate directory for corporate/private CAs.

If set and points to a directory, all .pem/.crt/.cer/.ca-bundle files
in that directory are trusted in addition to system CAs.
"""

# Daft analytics are disabled via ENV vars in the Dockerfile (DO_NOT_TRACK,
# SCARF_NO_ANALYTICS, DAFT_ANALYTICS_ENABLED). They must NOT be set here at
# module level — os.environ assignments call os.putenv(), which Temporal's
# workflow sandbox flags as non-deterministic, causing worker eviction loops.
# See: https://github.com/atlanhq/application-sdk/pull/1129
