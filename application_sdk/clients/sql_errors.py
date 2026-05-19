"""Typed error leaves for the SQL client family."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    AuthError,
    InternalError,
    InvalidInputError,
    UnimplementedError,
)


@dataclass(kw_only=True)
class SqlClientConfigError(InternalError):
    """DB_CONFIG is not set on this SQL client subclass."""

    code: ClassVar[str] = "INTERNAL_SQL_DB_CONFIG_NOT_SET"
    message: str = "DB_CONFIG is not configured for this SQL client"
    component: str | None = "sql_client"


@dataclass(kw_only=True)
class SqlClientAuthFailedError(AuthError):
    """SQL database authentication failed."""

    code: ClassVar[str] = "AUTH_SQL_CLIENT_FAILED"
    message: str = "SQL client authentication failed"
    auth_method: str | None = "sql"


@dataclass(kw_only=True)
class SqlCredentialsParseError(InvalidInputError):
    """SQL credentials could not be parsed or are missing required fields."""

    code: ClassVar[str] = "INVALID_INPUT_CREDENTIALS_PARSE"
    message: str = "SQL credentials parse error"


@dataclass(kw_only=True)
class SqlAwsCredentialsError(AuthError):
    """AWS IAM credentials required for RDS authentication are missing."""

    code: ClassVar[str] = "AUTH_AWS_CREDENTIALS"
    message: str = "AWS IAM credentials are missing or incomplete"
    auth_method: str | None = "aws_iam"


@dataclass(kw_only=True)
class MissingSqlParamError(InvalidInputError):
    """A required SQL connection parameter is missing."""

    code: ClassVar[str] = "INVALID_INPUT_SQL_MISSING_PARAM"
    message: str = "Required SQL connection parameter is missing"


@dataclass(kw_only=True)
class EngineNotInitializedError(InternalError):
    """SQL engine is not initialized; call load() before running queries."""

    code: ClassVar[str] = "INTERNAL_SQL_ENGINE_NOT_INITIALIZED"
    message: str = "Engine is not initialized. Call load() first."
    component: str | None = "sql_engine"


@dataclass(kw_only=True)
class UnsupportedSqlCursorError(UnimplementedError):
    """The current SQL engine or connection does not support cursors."""

    code: ClassVar[str] = "UNIMPLEMENTED_SQL_CURSOR_TYPE"
    message: str = "Cursor is not supported by this SQL engine"


@dataclass(kw_only=True)
class InvalidSqlEngineTypeError(InternalError):
    """The engine is a raw connection string, not a SQLAlchemy engine object."""

    code: ClassVar[str] = "INTERNAL_SQL_INVALID_ENGINE_TYPE"
    message: str = (
        "Engine should be an SQLAlchemy engine object, not a connection string"
    )
    component: str | None = "sql_engine"


@dataclass(kw_only=True)
class SqlPandasResultError(InternalError):
    """A SQL read operation failed to produce a valid pandas DataFrame result."""

    code: ClassVar[str] = "INTERNAL_SQL_PANDAS"
    message: str = "Error reading data from SQL into a pandas DataFrame"
    component: str | None = "sql_pandas"


@dataclass(kw_only=True)
class AbstractClientLoadError(UnimplementedError):
    """load() called on a ClientInterface subclass that has not implemented it."""

    code: ClassVar[str] = "UNIMPLEMENTED_CLIENT_LOAD"
    message: str = "load method is not implemented"
