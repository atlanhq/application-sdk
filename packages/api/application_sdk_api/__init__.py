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

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# Every public name resolves lazily (PEP 562). Importing ``application_sdk_api`` —
# or ``application_sdk.errors``, which re-exports ``application_sdk_api.errors`` —
# must not pull FastAPI, the server assembly or the SQL client into a process that
# only wants the error taxonomy (the Temporal workflow sandbox imports errors).
_LAZY: dict[str, str] = {
    "DatabaseConfig": "application_sdk_api.clients.models",
    "BaseSQLClient": "application_sdk_api.clients.sql",
    "ConfigStore": "application_sdk_api.config",
    "LocalFileConfigStore": "application_sdk_api.config",
    "config_objectstore_key": "application_sdk_api.config",
    "AppError": "application_sdk_api.errors",
    "AuthError": "application_sdk_api.errors",
    "DependencyUnavailableError": "application_sdk_api.errors",
    "FailureDetails": "application_sdk_api.errors",
    "InternalError": "application_sdk_api.errors",
    "InvalidInputError": "application_sdk_api.errors",
    "ApiMetadataObject": "application_sdk_api.handler",
    "ApiMetadataOutput": "application_sdk_api.handler",
    "AuthInput": "application_sdk_api.handler",
    "AuthOutput": "application_sdk_api.handler",
    "AuthStatus": "application_sdk_api.handler",
    "BaseConnectionConfig": "application_sdk_api.handler",
    "BaseMetadataConfig": "application_sdk_api.handler",
    "DefaultHandler": "application_sdk_api.handler",
    "Handler": "application_sdk_api.handler",
    "HandlerCredential": "application_sdk_api.handler",
    "MetadataInput": "application_sdk_api.handler",
    "MetadataOutput": "application_sdk_api.handler",
    "PreflightCheck": "application_sdk_api.handler",
    "PreflightInput": "application_sdk_api.handler",
    "PreflightOutput": "application_sdk_api.handler",
    "PreflightStatus": "application_sdk_api.handler",
    "SqlMetadataObject": "application_sdk_api.handler",
    "SqlMetadataOutput": "application_sdk_api.handler",
    "SQLHandler": "application_sdk_api.handler.sql",
    "ServerRevision": "application_sdk_api.revision",
    "compute_server_revision": "application_sdk_api.revision",
    "server_revision": "application_sdk_api.revision",
    "source_digest_from_tree": "application_sdk_api.revision",
    "build_asgi_app": "application_sdk_api.server",
    "WORKFLOW_EXTRA_AVAILABLE": "application_sdk_api.workflow",
    "StartRequest": "application_sdk_api.workflow",
    "StartResult": "application_sdk_api.workflow",
    "WorkflowStarter": "application_sdk_api.workflow",
    "starter_from_env": "application_sdk_api.workflow",
    "HandlerError": "application_sdk_api.handler.base",
    "F401": "application_sdk_api.errors",
    "skip": "application_sdk_api.errors",
}

if TYPE_CHECKING:
    from application_sdk_api.clients.models import DatabaseConfig  # noqa: F401
    from application_sdk_api.clients.sql import BaseSQLClient  # noqa: F401
    from application_sdk_api.config import (  # noqa: F401
        ConfigStore,
        LocalFileConfigStore,
        config_objectstore_key,
    )
    from application_sdk_api.errors import (  # noqa: F401
        F401,
        AppError,
        AuthError,
        DependencyUnavailableError,
        FailureDetails,
        InternalError,
        InvalidInputError,
        skip,
    )
    from application_sdk_api.handler import (  # noqa: F401
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
    from application_sdk_api.handler.base import HandlerError  # noqa: F401
    from application_sdk_api.handler.sql import SQLHandler  # noqa: F401
    from application_sdk_api.revision import (  # noqa: F401
        ServerRevision,
        compute_server_revision,
        server_revision,
        source_digest_from_tree,
    )
    from application_sdk_api.server import build_asgi_app  # noqa: F401
    from application_sdk_api.workflow import (  # noqa: F401
        WORKFLOW_EXTRA_AVAILABLE,
        StartRequest,
        StartResult,
        WorkflowStarter,
        starter_from_env,
    )

__version__ = "3.39.1"

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
    # build identity
    "ServerRevision",
    "compute_server_revision",
    "server_revision",
    "source_digest_from_tree",
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
    "AuthError",
    "InvalidInputError",
    "InternalError",
    "DependencyUnavailableError",
    "FailureDetails",
]


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'application_sdk_api' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
