"""Typed error leaves for SqlApp template."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.categories import Audience
from application_sdk.errors.leaves import AppTimeoutError, UnimplementedError


@dataclass(kw_only=True)
class MapDatabaseUnimplementedError(UnimplementedError):
    """map_database() not overridden in SqlApp subclass."""

    code: ClassVar[str] = "UNIMPLEMENTED_SQL_APP_MAP_DATABASE"
    message: str = "Override map_database() in your SqlApp subclass"
    operation: str | None = "map_database"


@dataclass(kw_only=True)
class MapSchemaUnimplementedError(UnimplementedError):
    """map_schema() not overridden in SqlApp subclass."""

    code: ClassVar[str] = "UNIMPLEMENTED_SQL_APP_MAP_SCHEMA"
    message: str = "Override map_schema() in your SqlApp subclass"
    operation: str | None = "map_schema"


@dataclass(kw_only=True)
class MapTableUnimplementedError(UnimplementedError):
    """map_table() not overridden in SqlApp subclass."""

    code: ClassVar[str] = "UNIMPLEMENTED_SQL_APP_MAP_TABLE"
    message: str = "Override map_table() in your SqlApp subclass"
    operation: str | None = "map_table"


@dataclass(kw_only=True)
class MapColumnUnimplementedError(UnimplementedError):
    """map_column() not overridden in SqlApp subclass."""

    code: ClassVar[str] = "UNIMPLEMENTED_SQL_APP_MAP_COLUMN"
    message: str = "Override map_column() in your SqlApp subclass"
    operation: str | None = "map_column"


@dataclass(kw_only=True)
class MapProcedureUnimplementedError(UnimplementedError):
    """map_procedure() not overridden in SqlApp subclass."""

    code: ClassVar[str] = "UNIMPLEMENTED_SQL_APP_MAP_PROCEDURE"
    message: str = "Override map_procedure() in your SqlApp subclass"
    operation: str | None = "map_procedure"


@dataclass(kw_only=True)
class SqlClientClassNotSetError(UnimplementedError):
    """sql_client_class not set on SqlApp subclass."""

    code: ClassVar[str] = "UNIMPLEMENTED_SQL_CLIENT_CLASS_NOT_SET"
    message: str = "sql_client_class must be set on the SqlApp subclass"
    operation: str | None = "sql_client_class"


@dataclass(kw_only=True)
class SqlProbeTimeoutError(AppTimeoutError):
    """The SQL auth-cache prime probe ran out of time waiting on the source.

    ``AppTimeoutError`` defaults to ``APP_OWNER`` because a timeout's locus is
    usually ambiguous — a source network read, a Temporal activity deadline and
    a heartbeat timeout route to three different people, and the base class
    cannot tell them apart. Here it is not ambiguous, so this leaf picks, as
    the base class docstring instructs.

    Only Python-level timeouts raised by the driver or the socket reach this
    error: connect timeout, login timeout, read deadline. In each one the clock
    ran out waiting for the customer's source. The Temporal-deadline case that
    would justify ``APP_OWNER`` cannot arrive — a start-to-close overrun kills
    the activity from outside, so ``prime_sql_auth`` never returns and never
    classifies (see its ``retry_max_attempts=1`` note).
    """

    code: ClassVar[str] = "TIMEOUT_SQL_PROBE"
    audience: ClassVar[Audience] = Audience.USER
    operation: str | None = "prime_sql_auth"
