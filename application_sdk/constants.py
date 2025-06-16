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

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

# Application Constants
#: Name of the application, used for identification
APPLICATION_NAME = os.getenv("ATLAN_APPLICATION_NAME", "default")
#: Host address for the application's HTTP server
APP_HOST = str(os.getenv("ATLAN_APP_HTTP_HOST", "localhost"))
#: Port number for the application's HTTP server
APP_PORT = int(os.getenv("ATLAN_APP_HTTP_PORT", "8000"))
#: Tenant ID for multi-tenant applications
APP_TENANT_ID = os.getenv("ATLAN_TENANT_ID", "default")
#: Host address for the application's dashboard
APP_DASHBOARD_HOST = str(os.getenv("ATLAN_APP_DASHBOARD_HOST", "localhost"))
#: Port number for the application's dashboard
APP_DASHBOARD_PORT = int(os.getenv("ATLAN_APP_DASHBOARD_PORT", "8000"))
#: Minimum required SQL Server version
SQL_SERVER_MIN_VERSION = os.getenv("ATLAN_SQL_SERVER_MIN_VERSION")
#: Path to the SQL queries directory
SQL_QUERIES_PATH = os.getenv("ATLAN_SQL_QUERIES_PATH", "app/sql")

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

# Workflow Constants
#: Timeout duration for activity heartbeats
HEARTBEAT_TIMEOUT = timedelta(
    seconds=int(os.getenv("ATLAN_HEARTBEAT_TIMEOUT", 300))  # 5 minutes
)
#: Maximum duration an activity can run before timing out
START_TO_CLOSE_TIMEOUT = timedelta(
    seconds=int(os.getenv("ATLAN_START_TO_CLOSE_TIMEOUT", 2 * 60 * 60))  # 2 hours
)

# SQL Client Constants
#: Whether to use server-side cursors for SQL operations
USE_SERVER_SIDE_CURSOR = bool(os.getenv("ATLAN_SQL_USE_SERVER_SIDE_CURSOR", "true"))

# DAPR Constants
#: Name of the state store component in DAPR
STATE_STORE_NAME = os.getenv("STATE_STORE_NAME", "statestore")
#: Name of the secret store component in DAPR
SECRET_STORE_NAME = os.getenv("SECRET_STORE_NAME", "secretstore")
#: Name of the object store component in DAPR
OBJECT_STORE_NAME = os.getenv("OBJECT_STORE_NAME", "objectstore")
#: Name of the pubsub component in DAPR
EVENT_STORE_NAME = os.getenv("EVENT_STORE_NAME", "eventstore")

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
#: Whether to enable OpenTelemetry log export
ENABLE_OTLP_LOGS: bool = os.getenv("ENABLE_OTLP_LOGS", "false").lower() == "true"

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

# Observability Constants
#: Directory for storing observability data
OBSERVABILITY_DIR = os.environ.get("ATLAN_OBSERVABILITY_DIR", "/tmp/observability")

# Log batching configuration
LOG_BATCH_SIZE = int(os.environ.get("ATLAN_LOG_BATCH_SIZE", 100))
LOG_FLUSH_INTERVAL_SECONDS = int(os.environ.get("ATLAN_LOG_FLUSH_INTERVAL_SECONDS", 10))

# Log Retention configuration
LOG_RETENTION_DAYS = int(os.environ.get("ATLAN_LOG_RETENTION_DAYS", 30))
LOG_CLEANUP_ENABLED = bool(os.environ.get("ATLAN_LOG_CLEANUP_ENABLED", False))

# Log Location configuration
LOG_DIR = os.environ.get("ATLAN_LOG_DIR", "/tmp/observability")
LOG_FILE_NAME = os.environ.get("ATLAN_LOG_FILE_NAME", "log.parquet")
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
    os.getenv("ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK", "false").lower() == "true"
)
