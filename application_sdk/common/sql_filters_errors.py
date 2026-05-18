"""Typed error leaves for the SQL filters module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError


@dataclass(kw_only=True)
class InvalidSqlFilterError(InvalidInputError):
    """Filter input could not be parsed as valid JSON."""

    code: ClassVar[str] = "INVALID_INPUT_SQL_FILTER_JSON"
    message: str = "Invalid filter JSON"
    field: str | None = "filter"
