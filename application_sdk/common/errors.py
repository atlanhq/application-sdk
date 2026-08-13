"""Typed error leaves for common utility modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    DataIntegrityError,
    InvalidInputError,
    UnimplementedError,
)


@dataclass(kw_only=True)
class JsonParseError(DataIntegrityError):
    """JSON string could not be parsed."""

    code: ClassVar[str] = "DATA_INTEGRITY_JSON_READ"
    message: str = "Invalid JSON string"
    expectation: str | None = "valid_json"


@dataclass(kw_only=True)
class ResponseKeyMissingError(InvalidInputError):
    """Response dict is missing expected key."""

    code: ClassVar[str] = "INVALID_INPUT_RESPONSE_MISSING_KEY"
    message: str = "Response dict is missing required key"
    field: str | None = "key"


@dataclass(kw_only=True)
class ResponseTypeError(InvalidInputError):
    """Response has unsupported type."""

    code: ClassVar[str] = "INVALID_INPUT_RESPONSE_TYPE"
    message: str = "Unsupported response type"
    field: str | None = "response"


@dataclass(kw_only=True)
class PathEmptyError(InvalidInputError):
    """Path argument is empty string."""

    code: ClassVar[str] = "INVALID_INPUT_PATH_EMPTY"
    message: str = "Path cannot be empty"
    field: str | None = "path"


@dataclass(kw_only=True)
class AtomicWriteModeError(InvalidInputError):
    """``atomic_write`` was given a mode it cannot honour.

    An append mode is the case worth naming: the staging file starts empty, so
    appending to it would discard the existing artifact rather than extend it —
    silently, and only for callers who happen to append. Raised eagerly so that
    mistake is a startup-shaped failure rather than a data-loss one.
    """

    code: ClassVar[str] = "INVALID_INPUT_ATOMIC_WRITE_MODE"
    message: str = "Unsupported mode for an atomic write"
    field: str | None = "mode"


@dataclass(kw_only=True)
class FileConverterNotFoundError(UnimplementedError):
    """No converter registered for the given file type."""

    code: ClassVar[str] = "UNIMPLEMENTED_FILE_CONVERTER"
    message: str = "No converter found for file type"
    operation: str | None = "file_conversion"
