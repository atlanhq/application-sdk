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
