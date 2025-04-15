import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

# Application Constants
APPLICATION_NAME = os.getenv("ATLAN_APPLICATION_NAME", "default")
APP_HOST = str(os.getenv("ATLAN_APP_HTTP_HOST", "localhost"))
APP_PORT = int(os.getenv("ATLAN_APP_HTTP_PORT", "8000"))
APP_TENANT_ID = os.getenv("ATLAN_TENANT_ID", "default")
APP_DASHBOARD_HOST = str(os.getenv("ATLAN_APP_DASHBOARD_HOST", "localhost"))
APP_DASHBOARD_PORT = int(os.getenv("ATLAN_APP_DASHBOARD_PORT", "8000"))
SQL_SERVER_MIN_VERSION = os.getenv("ATLAN_SQL_SERVER_MIN_VERSION")


# Workflow Client Constants
WORKFLOW_HOST = os.getenv("ATLAN_WORKFLOW_HOST", "localhost")
WORKFLOW_PORT = os.getenv("ATLAN_WORKFLOW_PORT", "7233")
WORKFLOW_NAMESPACE = os.getenv("ATLAN_WORKFLOW_NAMESPACE", "default")
WORKFLOW_UI_HOST = os.getenv("ATLAN_WORKFLOW_UI_HOST", "localhost")
WORKFLOW_UI_PORT = os.getenv("ATLAN_WORKFLOW_UI_PORT", "8233")
WORKFLOW_MAX_TIMEOUT_HOURS = timedelta(
    hours=int(os.getenv("ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS", "1"))
)
MAX_CONCURRENT_ACTIVITIES = int(os.getenv("ATLAN_MAX_CONCURRENT_ACTIVITIES", "5"))

# Workflow Constants
HEARTBEAT_TIMEOUT = timedelta(
    seconds=int(os.getenv("ATLAN_HEARTBEAT_TIMEOUT", 300))  # 2 minutes
)
START_TO_CLOSE_TIMEOUT = timedelta(
    seconds=int(os.getenv("ATLAN_START_TO_CLOSE_TIMEOUT", 2 * 60 * 60))  # 2 hours
)

# SQL Client Constants
USE_SERVER_SIDE_CURSOR = bool(os.getenv("ATLAN_SQL_USE_SERVER_SIDE_CURSOR", "true"))

# DAPR Constants
STATE_STORE_NAME = os.getenv("STATE_STORE_NAME", "statestore")
SECRET_STORE_NAME = os.getenv("SECRET_STORE_NAME", "secretstore")
OBJECT_STORE_NAME = os.getenv("OBJECT_STORE_NAME", "objectstore")

# Logger Constants
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "application-sdk")
SERVICE_VERSION: str = os.getenv("OTEL_SERVICE_VERSION", "0.1.0")
OTEL_RESOURCE_ATTRIBUTES: str = os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")
OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
)
ENABLE_OTLP_LOGS: bool = os.getenv("ENABLE_OTLP_LOGS", "false").lower() == "true"

# OTEL Constants
OTEL_WF_NODE_NAME = os.getenv("OTEL_WF_NODE_NAME", "")
OTEL_EXPORTER_TIMEOUT_SECONDS = int(os.getenv("OTEL_EXPORTER_TIMEOUT_SECONDS", "30"))
OTEL_BATCH_DELAY_MS = int(os.getenv("OTEL_BATCH_DELAY_MS", "5000"))
OTEL_BATCH_SIZE = int(os.getenv("OTEL_BATCH_SIZE", "512"))
OTEL_QUEUE_SIZE = int(os.getenv("OTEL_QUEUE_SIZE", "2048"))
