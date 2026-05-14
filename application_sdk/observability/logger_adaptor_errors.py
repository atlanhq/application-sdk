"""Typed error leaves for the logger adaptor module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InternalError


@dataclass(kw_only=True)
class UnsupportedLogRecordError(InternalError):
    """The log record format passed to the adaptor is not recognised."""

    code: ClassVar[str] = "INTERNAL_LOGGER_UNSUPPORTED_RECORD_FORMAT"
    message: str = "Unsupported log record format"
    component: str | None = "logger_adaptor"
    observed_type: str | None = None
