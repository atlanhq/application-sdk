"""Storage error classes."""

from __future__ import annotations

from typing import ClassVar

from application_sdk.errors import (
    STORAGE_CONFIG,
    STORAGE_NOT_FOUND,
    STORAGE_OPERATION,
    STORAGE_PERMISSION,
    ErrorCode,
)


class StorageError(Exception):
    """Base class for storage errors."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_OPERATION

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.key = key
        self.cause = cause
        self._error_code = error_code

    @property
    def error_code(self) -> ErrorCode:
        return (
            self._error_code
            if self._error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        parts = [f"[{self.error_code.code}] {self.message}"]
        if self.key:
            parts.append(f"key={self.key}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


class StorageNotFoundError(StorageError):
    """Raised when a requested key does not exist in the store."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_NOT_FOUND


class StoragePermissionError(StorageError):
    """Raised when the caller lacks permission to access a key."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_PERMISSION


class StorageConfigError(StorageError):
    """Raised when the store cannot be configured (bad YAML, missing bucket, etc.)."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_CONFIG
