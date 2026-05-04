"""Storage error classes.

Legacy classes below are deprecated shims that preserve back-compat for v3.x.
Migrate to ``application_sdk.errors.*``. All legacy names are removed in v4.0.
"""

from __future__ import annotations

import warnings
from typing import ClassVar

from application_sdk.errors import (
    STORAGE_CONFIG,
    STORAGE_NOT_FOUND,
    STORAGE_OPERATION,
    STORAGE_PERMISSION,
    ErrorCode,
)
from application_sdk.errors.leaves import (
    DependencyUnavailableError,
    InvalidInputError,
    NotFoundError,
    PermissionError,
)


class StorageError(DependencyUnavailableError):
    """Deprecated: use ``application_sdk.errors.DependencyUnavailableError`` — removed in v4.0."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_OPERATION
    code: ClassVar[str] = "STORAGE"

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "StorageError is deprecated; use application_sdk.errors.DependencyUnavailableError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        DependencyUnavailableError.__init__(self, message=message, cause=cause)
        self.key = key
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


class StorageNotFoundError(NotFoundError):
    """Deprecated: use ``application_sdk.errors.NotFoundError`` — removed in v4.0."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_NOT_FOUND
    code: ClassVar[str] = "STORAGE_NOT_FOUND"

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "StorageNotFoundError is deprecated; use application_sdk.errors.NotFoundError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        NotFoundError.__init__(self, message=message, cause=cause)
        self.key = key
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


class StoragePermissionError(PermissionError):
    """Deprecated: use ``application_sdk.errors.PermissionError`` — removed in v4.0."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_PERMISSION
    code: ClassVar[str] = "STORAGE_PERMISSION"

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "StoragePermissionError is deprecated; use application_sdk.errors.PermissionError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        PermissionError.__init__(self, message=message, cause=cause)
        self.key = key
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


class StorageConfigError(InvalidInputError):
    """Deprecated: use ``application_sdk.errors.InvalidInputError`` — removed in v4.0."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_CONFIG
    code: ClassVar[str] = "STORAGE_CONFIG"

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "StorageConfigError is deprecated; use application_sdk.errors.InvalidInputError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        InvalidInputError.__init__(self, message=message, cause=cause)
        self.key = key
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
