"""Typed error leaves for the integration test runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError


@dataclass(kw_only=True)
class ValidationInputError(InvalidInputError):
    """A pandera / record-count validation check received invalid input."""

    code: ClassVar[str] = "INVALID_INPUT_VALIDATION"


@dataclass(kw_only=True)
class ComparisonInputError(InvalidInputError):
    """Expected-data file for asset comparison has an invalid structure."""

    code: ClassVar[str] = "INVALID_INPUT_COMPARISON"


@dataclass(kw_only=True)
class HttpClientInputError(InvalidInputError):
    """Unsupported API type passed to the integration HTTP client."""

    code: ClassVar[str] = "INVALID_INPUT_HTTP_CLIENT"
    field: str | None = "api_type"
