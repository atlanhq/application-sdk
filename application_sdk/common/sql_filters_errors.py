"""Typed error leaves for the SQL filters module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError, InvalidInputValueError


@dataclass(kw_only=True)
class InvalidSqlFilterError(InvalidInputError):
    """Filter input could not be parsed as valid JSON."""

    code: ClassVar[str] = "INVALID_INPUT_SQL_FILTER_JSON"
    message: str = "Invalid filter JSON"
    field: str | None = "filter"


@dataclass(kw_only=True)
class InvalidSqlFilterValueError(InvalidInputValueError):
    """A filter value was refused before SQL substitution on a raw-dict path.

    Raised by ``prepare_query``, ``prepare_filters`` and
    ``get_database_names`` when a filter fails the SQL deny-list or the
    regex-structure check. The value is the customer's own configuration, so
    the audience is ``USER``. Still a ``ValueError`` so callers that guarded
    the earlier bare ``ValueError`` keep working. The message names the
    problem, never the value.
    """

    code: ClassVar[str] = "INVALID_INPUT_SQL_FILTER_VALUE"
    message: str = "Filter value rejected"
    field: str | None = "filter"
