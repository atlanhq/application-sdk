from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import (
    AuthError,
    InternalError,
    InvalidInputError,
    UnimplementedError,
)

# === Recurring patterns — bake message + evidence ===


@dataclass(kw_only=True)
class SqlDbConfigNotSetError(InternalError):
    """DB_CONFIG was never set on the subclass — SDK contract violation."""

    code: ClassVar[str] = "INTERNAL_SQL_DB_CONFIG_NOT_SET"
    message: str = "DB_CONFIG is not configured for this SQL client."
    component: str | None = "sql_client"
    invariant: str | None = "db_config_required_for_subclass"


@dataclass(kw_only=True)
class SqlEngineNotInitializedError(InternalError):
    """`load()` was never called before a method that requires `self.engine`."""

    code: ClassVar[str] = "INTERNAL_SQL_ENGINE_NOT_INITIALIZED"
    message: str = "Engine is not initialized. Call load() first."
    component: str | None = "sql_client"
    invariant: str | None = "load_before_use"


@dataclass(kw_only=True)
class SqlClientAuthFailedError(AuthError):
    """SQLAlchemy engine creation / credential validation failed."""

    code: ClassVar[str] = "AUTH_SQL_CLIENT_FAILED"
    message: str = "SQL client authentication failed"
    auth_method: str | None = "sql"
    failure_reason: str | None = "engine_load_failed"


@dataclass(kw_only=True)
class SqlMissingCredentialFieldError(InvalidInputError):
    """A required credential field (username / database / aws_role_arn) is absent."""

    code: ClassVar[str] = "INVALID_INPUT_SQL_MISSING_CREDENTIAL_FIELD"
    # message= and field= supplied per raise site


# === One-off subclasses — bake code (and message where stable) ===


@dataclass(kw_only=True)
class SqlMissingRequiredParamError(InvalidInputError):
    """A required connection-string parameter is absent from credentials."""

    code: ClassVar[str] = "INVALID_INPUT_SQL_MISSING_REQUIRED_PARAM"
    # message= and field= supplied per raise site


@dataclass(kw_only=True)
class SqlCursorNotSupportedError(UnimplementedError):
    """DBAPI cursor has no `.description`; driver capability gap."""

    code: ClassVar[str] = "UNIMPLEMENTED_SQL_CURSOR_TYPE"
    message: str = "Cursor is not supported"
    operation: str | None = "sql_run_query"
    reason: str | None = "dbapi_cursor_unavailable"


@dataclass(kw_only=True)
class SqlEngineTypeError(InternalError):
    """`self.engine` is a string when an SQLAlchemy engine object was expected."""

    code: ClassVar[str] = "INTERNAL_SQL_ENGINE_NOT_SQLALCHEMY"
    message: str = "Engine should be an SQLAlchemy engine object"
    component: str | None = "sql_client"
    invariant: str | None = "engine_is_sqlalchemy_object"


@dataclass(kw_only=True)
class SqlPandasResultError(InternalError):
    """`pd.read_sql_query` returned a non-DataFrame value."""

    code: ClassVar[str] = "INTERNAL_SQL_PANDAS_RESULT_NOT_DATAFRAME"
    message: str = "Unable to get pandas dataframe from SQL query results"
    component: str | None = "sql_client"
    invariant: str | None = "pandas_read_sql_returns_dataframe"


@dataclass(kw_only=True)
class SqlInvalidAuthTypeError(InvalidInputError):
    """`authType` is not one of {basic, iam_user, iam_role}."""

    code: ClassVar[str] = "INVALID_INPUT_SQL_AUTH_TYPE"
    # message= and field= supplied per raise site


# === rewrap replacements (per §5 SQL_QUERY_* family) ===


@dataclass(kw_only=True)
class SqlBatchReadError(InternalError):
    """Pandas batched read via `get_batched_results` failed."""

    code: ClassVar[str] = "INTERNAL_SQL_PANDAS_BATCH"
    message: str = "Error reading batched data(pandas) from SQL"
    component: str | None = "sql_client"


@dataclass(kw_only=True)
class SqlReadError(InternalError):
    """Pandas single-frame read via `get_results` failed."""

    code: ClassVar[str] = "INTERNAL_SQL_PANDAS"
    message: str = "Error reading data(pandas) from SQL"
    component: str | None = "sql_client"


@dataclass(kw_only=True)
class SqlExecuteQueryError(InternalError):
    """Async query execution inside `AsyncBaseSQLClient.run_query` failed."""

    code: ClassVar[str] = "INTERNAL_SQL_QUERY_EXECUTE"
    message: str = "Error executing query"
    component: str | None = "sql_client"
