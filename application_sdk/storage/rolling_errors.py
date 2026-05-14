"""Typed error leaves for the rolling file writer module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError, PreconditionError


@dataclass(kw_only=True)
class InvalidRolloverPolicyError(InvalidInputError):
    """A rollover policy parameter value is invalid."""

    code: ClassVar[str] = "INVALID_INPUT_ROLLOVER_POLICY"
    message: str = "Rollover policy parameter is invalid"
    field: str | None = None
    received_value: str | None = None


@dataclass(kw_only=True)
class InvalidRollingFileWriterError(InvalidInputError):
    """A RollingFileWriter constructor parameter is invalid."""

    code: ClassVar[str] = "INVALID_INPUT_ROLLING_WRITER"
    message: str = "RollingFileWriter parameter is invalid"
    field: str | None = None
    received_value: str | None = None


@dataclass(kw_only=True)
class RollingWriterClosedError(PreconditionError):
    """An append was attempted on a closed RollingFileWriter."""

    code: ClassVar[str] = "PRECONDITION_ROLLING_WRITER_CLOSED"
    message: str = "Cannot append to a closed RollingFileWriter"
    resource: str | None = "rolling_file_writer"
