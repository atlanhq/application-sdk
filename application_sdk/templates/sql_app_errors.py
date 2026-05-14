"""Typed error leaves for SqlApp template."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import UnimplementedError


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
