"""Application SDK configuration constants.

This module contains all the configuration constants used throughout the Application SDK.
Constants are primarily loaded from environment variables with sensible defaults.

The constants are organized into the following categories:
- Application Configuration
- Workflow Configuration
- SQL Client Configuration
- DAPR Configuration
- Logging Configuration
- OpenTelemetry Configuration

Example:
    >>> from application_sdk.constants import APPLICATION_NAME, WORKFLOW_HOST
    >>> print(f"Running application {APPLICATION_NAME} on {WORKFLOW_HOST}")

Note:
    Most constants can be configured via environment variables. See the .env.example
    file for all available configuration options.
"""

import os
from datetime import timedelta
from enum import Enum

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

# Static Constants
LOCAL_ENVIRONMENT = "local"

# Application Constants
#: Name of the application, used for identification
APPLICATION_NAME = os.getenv("ATLAN_APPLICATION_NAME", "default")
#: Name of the deployment, used to distinguish between different deployments of the same application
DEPLOYMENT_NAME = os.getenv("ATLAN_DEPLOYMENT_NAME", LOCAL_ENVIRONMENT)
#: Host address for the application's HTTP server
APP_HOST = str(os.getenv("ATLAN_APP_HTTP_HOST", "0.0.0.0"))
#: Port number for the application's HTTP server
APP_PORT = int(os.getenv("ATLAN_APP_HTTP_PORT", "8000"))
#: Tenant ID for multi-tenant applications
APP_TENANT_ID = os.getenv("ATLAN_TENANT_ID", "default")
# Domain Name of the tenant
DOMAIN_NAME = os.getenv("ATLAN_DOMAIN_NAME", "atlan.com")
#: Host address for the application's dashboard
APP_DASHBOARD_HOST = str(os.getenv("ATLAN_APP_DASHBOARD_HOST", "localhost"))
#: Port number for the application's dashboard
APP_DASHBOARD_PORT = int(os.getenv("ATLAN_APP_DASHBOARD_PORT", "8000"))
#: Minimum required SQL Server version
SQL_SERVER_MIN_VERSION = os.getenv("ATLAN_SQL_SERVER_MIN_VERSION")
#: Path to the SQL queries directory
SQL_QUERIES_PATH = os.getenv("ATLAN_SQL_QUERIES_PATH", "app/sql")

# Output Path Constants
#: Output path format for workflows.
#:
#: Example: objectstore://bucket/artifacts/apps/{application_name}/workflows/{workflow_id}/{workflow_run_id}
WORKFLOW_OUTPUT_PATH_TEMPLATE = (
    "artifacts/apps/{application_name}/workflows/{workflow_id}/{run_id}"
)

# Temporary Path (used to store intermediate files)
TEMPORARY_PATH = os.getenv("ATLAN_TEMPORARY_PATH", "./local/tmp/")

# Cleanup Paths (custom paths for cleanup operations, supports multiple paths separated by comma)
# If empty, cleanup activities will default to workflow-specific paths at runtime
CLEANUP_BASE_PATHS = [
    path.strip()
    for path in os.getenv("ATLAN_CLEANUP_BASE_PATHS", "").split(",")
    if path.strip()
]

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

# Workflow Client Constants
#: Host address for the Temporal server
WORKFLOW_HOST = os.getenv("ATLAN_WORKFLOW_HOST", "localhost")
#: Port number for the Temporal server
WORKFLOW_PORT = os.getenv("ATLAN_WORKFLOW_PORT", "7233")
#: Namespace for Temporal workflows
WORKFLOW_NAMESPACE = os.getenv("ATLAN_WORKFLOW_NAMESPACE", "default")
#: Host address for the Temporal UI
WORKFLOW_UI_HOST = os.getenv("ATLAN_WORKFLOW_UI_HOST", "localhost")
#: Port number for the Temporal UI
WORKFLOW_UI_PORT = os.getenv("ATLAN_WORKFLOW_UI_PORT", "8233")

#: Maximum timeout duration for workflows
WORKFLOW_MAX_TIMEOUT_HOURS = timedelta(
    hours=int(os.getenv("ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS", "1"))
)
#: Maximum number of activities that can run concurrently
MAX_CONCURRENT_ACTIVITIES = int(os.getenv("ATLAN_MAX_CONCURRENT_ACTIVITIES", "5"))


#: Name of the deployment secrets in the secret store
DEPLOYMENT_SECRET_PATH = os.getenv(
    "ATLAN_DEPLOYMENT_SECRET_PATH", "ATLAN_DEPLOYMENT_SECRETS"
)
AUTH_ENABLED = os.getenv("ATLAN_AUTH_ENABLED", "false").lower() == "true"
#: OAuth2 authentication URL for workflow services
AUTH_URL = os.getenv("ATLAN_AUTH_URL")
#: Whether to enable TLS for Temporal workflow connections
WORKFLOW_TLS_ENABLED = (
    os.getenv("ATLAN_WORKFLOW_TLS_ENABLED", "false").lower() == "true"
)

# Deployment Secret Store Key Names
#: Key name for OAuth2 client ID in deployment secrets (can be overridden via ATLAN_AUTH_CLIENT_ID_KEY)
WORKFLOW_AUTH_CLIENT_ID_KEY = os.getenv(
    "ATLAN_AUTH_CLIENT_ID_KEY", "ATLAN_AUTH_CLIENT_ID"
)
#: Key name for OAuth2 client secret in deployment secrets (can be overridden via ATLAN_AUTH_CLIENT_SECRET_KEY)
WORKFLOW_AUTH_CLIENT_SECRET_KEY = os.getenv(
    "ATLAN_AUTH_CLIENT_SECRET_KEY", "ATLAN_AUTH_CLIENT_SECRET"
)

# Workflow Constants
#: Timeout duration for activity heartbeats
HEARTBEAT_TIMEOUT = timedelta(
    seconds=int(os.getenv("ATLAN_HEARTBEAT_TIMEOUT_SECONDS", 300))  # 5 minutes
)
#: Maximum duration an activity can run before timing out
START_TO_CLOSE_TIMEOUT = timedelta(
    seconds=int(
        os.getenv("ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS", 2 * 60 * 60)
    )  # 2 hours
)

#: Graceful shutdown timeout for workers
#: This is the maximum time the worker will wait for in-flight activities to complete
#: before forcing shutdown when receiving SIGTERM/SIGINT signals.
#: The worker will exit early if all activities complete before this timeout.
GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = int(
    os.getenv("ATLAN_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS", 12 * 60 * 60)  # 12 hours
)

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
#: Log level for the application (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
#: Service name for OpenTelemetry
SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "atlan-application-sdk")
#: Service version for OpenTelemetry
SERVICE_VERSION: str = os.getenv("OTEL_SERVICE_VERSION", "0.1.0")
#: Additional resource attributes for OpenTelemetry
OTEL_RESOURCE_ATTRIBUTES: str = os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")
#: Endpoint for the OpenTelemetry collector
OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
)
#: Secondary endpoint for workflow logs (optional, for dual export to tenant-level collector)
OTEL_WORKFLOW_LOGS_ENDPOINT: str = os.getenv("OTEL_WORKFLOW_LOGS_ENDPOINT", "")
#: Whether to enable OpenTelemetry log export
ENABLE_OTLP_LOGS: bool = os.getenv("ENABLE_OTLP_LOGS", "false").lower() == "true"
#: Whether to enable workflow logs export to secondary endpoint (for S3 archival + live streaming)
ENABLE_WORKFLOW_LOGS_EXPORT: bool = (
    os.getenv("ENABLE_WORKFLOW_LOGS_EXPORT", "false").lower() == "true"
)

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
LOG_CLEANUP_ENABLED = bool(os.environ.get("ATLAN_LOG_CLEANUP_ENABLED", False))

# Log Location configuration
LOG_FILE_NAME = os.environ.get("ATLAN_LOG_FILE_NAME", "log.jsonl.gz")
# Hive Partitioning Configuration
ENABLE_HIVE_PARTITIONING = (
    os.getenv("ATLAN_ENABLE_HIVE_PARTITIONING", "true").lower() == "true"
)

# Metrics Configuration
ENABLE_OTLP_METRICS = os.getenv("ATLAN_ENABLE_OTLP_METRICS", "false").lower() == "true"
METRICS_FILE_NAME = "metrics.parquet"
METRICS_BATCH_SIZE = int(os.getenv("ATLAN_METRICS_BATCH_SIZE", "100"))
METRICS_FLUSH_INTERVAL_SECONDS = int(
    os.getenv("ATLAN_METRICS_FLUSH_INTERVAL_SECONDS", "10")
)
METRICS_CLEANUP_ENABLED = (
    os.getenv("ATLAN_METRICS_CLEANUP_ENABLED", "false").lower() == "true"
)
METRICS_RETENTION_DAYS = int(os.getenv("ATLAN_METRICS_RETENTION_DAYS", "30"))

# Segment Configuration
#: Segment API URL for sending events. Defaults to https://api.segment.io/v1/batch
SEGMENT_API_URL = os.getenv("ATLAN_SEGMENT_API_URL", "https://api.segment.io/v1/batch")
#: Segment write key for authentication
SEGMENT_WRITE_KEY = os.getenv("ATLAN_SEGMENT_WRITE_KEY", "")
#: Whether to enable Segment metrics export
ENABLE_SEGMENT_METRICS = (
    os.getenv("ATLAN_ENABLE_SEGMENT_METRICS", "false").lower() == "true"
)
#: Default user ID for Segment events
SEGMENT_DEFAULT_USER_ID = "atlan.automation"
#: Maximum batch size for Segment events
SEGMENT_BATCH_SIZE = int(os.getenv("ATLAN_SEGMENT_BATCH_SIZE", "100"))
#: Maximum time to wait before sending a batch (in seconds)
SEGMENT_BATCH_TIMEOUT_SECONDS = float(
    os.getenv("ATLAN_SEGMENT_BATCH_TIMEOUT_SECONDS", "10.0")
)

# Traces Configuration
ENABLE_OTLP_TRACES = os.getenv("ATLAN_ENABLE_OTLP_TRACES", "false").lower() == "true"
TRACES_BATCH_SIZE = int(os.getenv("ATLAN_TRACES_BATCH_SIZE", "100"))
TRACES_FLUSH_INTERVAL_SECONDS = int(
    os.getenv("ATLAN_TRACES_FLUSH_INTERVAL_SECONDS", "5")
)
TRACES_RETENTION_DAYS = int(os.getenv("ATLAN_TRACES_RETENTION_DAYS", "30"))
TRACES_CLEANUP_ENABLED = (
    os.getenv("ATLAN_TRACES_CLEANUP_ENABLED", "true").lower() == "true"
)
TRACES_FILE_NAME = "traces.parquet"

# Dapr Sink Configuration
ENABLE_OBSERVABILITY_DAPR_SINK = (
    os.getenv("ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK", "true").lower() == "true"
)

# atlan_client configuration (non ATLAN_ prefix are rooted in pyatlan SDK, to be revisited)
ATLAN_API_TOKEN_GUID = os.getenv("API_TOKEN_GUID")
ATLAN_BASE_URL = os.getenv("ATLAN_BASE_URL")
ATLAN_API_KEY = os.getenv("ATLAN_API_KEY")
ATLAN_CLIENT_ID = os.getenv("CLIENT_ID")
ATLAN_CLIENT_SECRET = os.getenv("CLIENT_SECRET")
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

# Disable Analytics Configuration for DAFT
os.environ["DO_NOT_TRACK"] = "true"
os.environ["SCARF_NO_ANALYTICS"] = "true"
os.environ["DAFT_ANALYTICS_ENABLED"] = "0"
