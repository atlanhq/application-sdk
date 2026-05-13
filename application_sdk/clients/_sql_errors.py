"""Typed SQL client error subclasses.

Each subclass encodes the static defaults (message, evidence fields) plus a
specific ``code`` that lands on the FailureDetails wire envelope. Raise sites
override only what is genuinely dynamic. Follows the WorkerEvictedError
precedent in application_sdk/errors/leaves.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import (
    AuthError,
    InternalError,
    InvalidInputError,
    UnimplementedError,
)


@dataclass(kw_only=True)
class DBConfigNotSetError(InternalError):
    """SQL client subclass did not set DB_CONFIG before use."""

    code: ClassVar[str] = "INTERNAL_DB_CONFIG_NOT_SET"
    message: str = "DB_CONFIG is not configured for this SQL client."
    component: str | None = "sql_client"
    invariant: str | None = "db_config_must_be_set"


@dataclass(kw_only=True)
class EngineNotInitializedError(InternalError):
    """run_query / _execute_query called before load() initialised the engine."""

    code: ClassVar[str] = "INTERNAL_ENGINE_NOT_INITIALIZED"
    message: str = "Engine is not initialized. Call load() first."
    component: str | None = "sql_client"
    invariant: str | None = "load_before_use"


@dataclass(kw_only=True)
class EngineWrongTypeError(InternalError):
    """Engine is a connection string when an SQLAlchemy engine object is required."""

    code: ClassVar[str] = "INTERNAL_ENGINE_WRONG_TYPE"
    message: str = "Engine should be an SQLAlchemy engine object, got str"
    component: str | None = "sql_client"
    invariant: str | None = "async_read_requires_engine_object"


@dataclass(kw_only=True)
class PandasResultTypeError(InternalError):
    """_execute_async_read_operation(query, None) returned a non-DataFrame."""

    code: ClassVar[str] = "INTERNAL_SQL_PANDAS"
    message: str = "Unable to get pandas dataframe from SQL query results"
    component: str | None = "sql_client"
    invariant: str | None = "get_results_returns_dataframe"


@dataclass(kw_only=True)
class SqlClientAuthFailedError(AuthError):
    """SQLAlchemy engine creation / first-connect failed during load().

    Raise sites typically pass only ``cause=exc`` — the upstream exception
    detail flows to the FailureDetails ``cause_repr`` via the base ``cause``
    field, so the message stays static.
    """

    code: ClassVar[str] = "AUTH_SQL_CLIENT_FAILED"
    message: str = "SQL client authentication failed"
    auth_method: str | None = "sql"
    failure_reason: str | None = "engine_load_failed"


@dataclass(kw_only=True)
class MissingRequiredFieldError(InvalidInputError):
    """Required credential / connection-string parameter not provided.

    Raise sites supply ``field=`` and an explicit ``message=`` that names
    the user-facing context (e.g. "for IAM user authentication"); the
    subclass only fixes the wire ``code``.
    """

    code: ClassVar[str] = "INVALID_INPUT_MISSING_FIELD"


@dataclass(kw_only=True)
class InvalidAuthTypeError(InvalidInputError):
    """credentials.authType not in {basic, iam_user, iam_role}."""

    code: ClassVar[str] = "INVALID_INPUT_AUTH_TYPE"
    field: str | None = "authType"
    constraint: str | None = "one_of:basic,iam_user,iam_role"


@dataclass(kw_only=True)
class UnsupportedCursorError(UnimplementedError):
    """DBAPI driver did not return a usable cursor for server-side cursor mode."""

    code: ClassVar[str] = "UNIMPLEMENTED_CURSOR_TYPE"
    message: str = "Cursor is not supported by this driver"
    operation: str | None = "sql_cursor"
    reason: str | None = "dbapi_cursor_unavailable"
