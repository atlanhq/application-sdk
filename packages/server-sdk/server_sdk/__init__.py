"""Atlan Server SDK — the lean, serving-only runtime for Atlan app servers.

Provides the auth / preflight / metadata / config / configmap handler surface, a
SQLAlchemy client for the connector auth path, a pluggable config store, and the
FastAPI assembly. The base install pulls none of the worker/data-processing
stack (temporalio, dapr, daft, duckdb, pandas, pyarrow, pyatlan, opentelemetry);
generic capability extras add just what a serving path needs — ``[sql]``
(SQLAlchemy), ``[aws]`` (boto3 + IAM helpers), ``[workflow]`` (temporalio, for a
standalone ``/start``). The SDK names no connectors; each app declares its own
driver dependencies.

``/workflows/v1/start`` and its temporalio dependency live behind the optional
``[workflow]`` extra and are registered only when it is installed: an app
running standalone starts workflows on its own worker, while the consolidated
serving image omits the extra and the route 404s.
"""

from server_sdk.clients.models import DatabaseConfig
from server_sdk.clients.sql import BaseSQLClient
from server_sdk.config import ConfigStore, LocalFileConfigStore, config_objectstore_key
from server_sdk.errors import (
    AppError,
    AuthError,
    DependencyUnavailableError,
    FailureDetails,
    HandlerError,
    InternalError,
    InvalidInputError,
)
from server_sdk.handler import (
    ApiMetadataObject,
    ApiMetadataOutput,
    AuthInput,
    AuthOutput,
    AuthStatus,
    BaseConnectionConfig,
    BaseMetadataConfig,
    DefaultHandler,
    Handler,
    HandlerCredential,
    MetadataInput,
    MetadataOutput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
)
from server_sdk.handler.sql import SQLHandler
from server_sdk.server import build_asgi_app
from server_sdk.workflow import (
    WORKFLOW_EXTRA_AVAILABLE,
    StartRequest,
    StartResult,
    WorkflowStarter,
    starter_from_env,
)

__version__ = "0.1.0"

__all__ = [
    # assembly
    "build_asgi_app",
    # handler
    "Handler",
    "DefaultHandler",
    "SQLHandler",
    # clients
    "BaseSQLClient",
    "DatabaseConfig",
    # config
    "ConfigStore",
    "LocalFileConfigStore",
    "config_objectstore_key",
    # workflow (start) seam
    "WORKFLOW_EXTRA_AVAILABLE",
    "WorkflowStarter",
    "StartRequest",
    "StartResult",
    "starter_from_env",
    # contracts
    "AuthInput",
    "AuthOutput",
    "AuthStatus",
    "BaseConnectionConfig",
    "BaseMetadataConfig",
    "HandlerCredential",
    "PreflightInput",
    "PreflightOutput",
    "PreflightCheck",
    "PreflightStatus",
    "MetadataInput",
    "MetadataOutput",
    "SqlMetadataObject",
    "SqlMetadataOutput",
    "ApiMetadataObject",
    "ApiMetadataOutput",
    # errors
    "AppError",
    "HandlerError",
    "AuthError",
    "InvalidInputError",
    "InternalError",
    "DependencyUnavailableError",
    "FailureDetails",
]
