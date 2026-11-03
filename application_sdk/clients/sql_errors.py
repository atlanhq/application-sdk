"""Typed error leaves for the SQL client family."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.categories import Audience
from application_sdk.errors.leaves import (
    AppTimeoutError,
    AuthError,
    InternalError,
    InvalidInputError,
    SourceUnavailableError,
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
class SqlClientSourceUnavailableError(SourceUnavailableError):
    """The SQL client could not reach the source while connecting.

    DNS failure, refused or filtered connection, TLS handshake failure: the
    credentials were never checked, so this is not an authentication problem
    and an account unlock will not fix it.
    """

    code: ClassVar[str] = "SOURCE_UNAVAILABLE_SQL_CLIENT"
    message: str = "SQL client could not reach the source"
    suggested_action: str | None = (
        "Check DNS resolution, firewall and private-link rules, TLS "
        "configuration, and that the source is accepting connections from "
        "the worker network."
    )
    source_type: str | None = "sql"


@dataclass(kw_only=True)
class SqlClientConnectTimeoutError(AppTimeoutError):
    """The SQL client's connect or login ran out of time waiting on the source.

    ``AppTimeoutError`` defaults to ``APP_OWNER`` because a timeout's locus is
    usually ambiguous. Here it is not: what times out is the driver's or the
    socket's own timeout, or ``load()``'s connect budget, spent building the
    connection string (an STS AssumeRole for IAM-role auth) and connecting —
    so the clock ran out waiting for the customer's source or its auth path.
    """

    code: ClassVar[str] = "TIMEOUT_SQL_CLIENT_CONNECT"
    audience: ClassVar[Audience] = Audience.USER
    message: str = "SQL client timed out connecting to the source"
    suggested_action: str | None = (
        "Check that the source is up and reachable from the worker network, "
        "and that the connection's login timeout suits the source."
    )
    operation: str | None = "sql_client_connect"


@dataclass(kw_only=True)
class SqlClientConnectUnclassifiedError(InternalError):
    """The SQL client failed to connect for a reason the classifier does not know.

    Raised instead of guessing at authentication. ``classification_pending``
    flags it so the driver error can be added to the shared SQL failure
    classifier rather than read at face value.
    """

    code: ClassVar[str] = "INTERNAL_SQL_CLIENT_CONNECT"
    message: str = "SQL client failed to connect for an unclassified reason"
    component: str | None = "sql_client"
    classification_pending: bool = True


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
class SqlBindParameterError(InternalError):
    """``run_query`` was given bind parameters it cannot apply.

    A name that is not an identifier, or one the query never references as
    ``:name``. Running anyway would execute the query without that value, so a
    filter meant to narrow the result would silently not apply.
    """

    code: ClassVar[str] = "INTERNAL_SQL_BIND_PARAMETER"
    message: str = "SQL bind parameters do not match the query"
    component: str | None = "sql_client"


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
    """A SQL read operation failed to produce a valid pandas DataFrame result.

    Raised in two distinct situations, which differ in retryability:

    - **Invariant violation** (the read path returned a non-DataFrame). A bug in
      our code; retrying cannot help. Keeps the ``INTERNAL`` default
      ``retryable=False``.
    - **Catch-and-rewrap of an unclassified driver exception** at the query
      boundary. Here the SDK has *not* established what failed — a transient
      source-side condition (concurrent DDL, lock contention, a dropped
      connection) is wrapped identically to a permanent one. Those sites pass
      ``retryable=True`` explicitly, which does not assert "this will succeed on
      retry"; it declines to override the activity's own declared Temporal retry
      policy for an error we never classified. Asserting ``retryable=False``
      there marks the failure ``non_retryable`` on the wire and makes the
      activity's declared ``maximum_attempts`` dead code (CONAT-747).

    Errors that *are* classified never reach either site — the enclosing
    ``except AppError: raise`` clauses re-raise them with their own retryability.
    """

    code: ClassVar[str] = "INTERNAL_SQL_PANDAS"
    message: str = "Error reading data from SQL into a pandas DataFrame"
    component: str | None = "sql_pandas"


@dataclass(kw_only=True)
class AbstractClientLoadError(UnimplementedError):
    """load() called on a ClientInterface subclass that has not implemented it."""

    code: ClassVar[str] = "UNIMPLEMENTED_CLIENT_LOAD"
    message: str = "load method is not implemented"
