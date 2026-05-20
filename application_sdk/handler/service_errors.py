"""Typed error leaves for the handler service module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InternalError, InvalidInputError


@dataclass(kw_only=True)
class TempPathEscapeError(InternalError):
    """A path escaped the system temp directory — invariant violation."""

    code: ClassVar[str] = "INTERNAL_HANDLER_TEMP_PATH_ESCAPE"
    message: str = "Temp file path escapes system temp directory"
    component: str | None = "handler"


@dataclass(kw_only=True)
class InvalidConfigIdError(InvalidInputError):
    """config_id did not match the allowed character pattern."""

    code: ClassVar[str] = "INVALID_INPUT_CONFIG_ID"
    message: str = "Invalid config_id: must match allowed character pattern"
    field: str | None = "config_id"
    config_id: str | None = None


@dataclass(kw_only=True)
class InvalidConfigTypeError(InvalidInputError):
    """config_type did not match the allowed character pattern."""

    code: ClassVar[str] = "INVALID_INPUT_CONFIG_TYPE"
    message: str = "Invalid config_type: must match allowed character pattern"
    field: str | None = "config_type"
    config_type: str | None = None
