"""Storage error hierarchy.

All storage-related exceptions inherit from StorageError, which provides
structured error information for debugging.

Exception Hierarchy:
    StorageError
    ├── StorageNotFoundError      - Object or bucket not found
    ├── StoragePermissionError    - Access denied
    ├── StorageConfigError        - Configuration/binding parse errors
    └── StorageOperationError     - General operation failures
"""

from __future__ import annotations

from typing import ClassVar


class StorageError(Exception):
    """Base exception for all storage operations.

    Provides structured error context for debugging and logging.
    """

    DEFAULT_ERROR_CODE: ClassVar[str] = "STORAGE_OPERATION"

    def __init__(
        self,
        message: str,
        *,
        store_type: str | None = None,
        path: str | None = None,
        operation: str | None = None,
        cause: Exception | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.store_type = store_type
        self.path = path
        self.operation = operation
        self.cause = cause
        self._error_code = error_code

    @property
    def error_code(self) -> str:
        """Structured error code for monitoring and alerting."""
        return (
            self._error_code
            if self._error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        parts = [f"[{self.error_code}] {self.message}"]
        if self.store_type:
            parts.append(f"store_type={self.store_type}")
        if self.path:
            parts.append(f"path={self.path}")
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


class StorageNotFoundError(StorageError):
    """Raised when an object or bucket does not exist."""

    DEFAULT_ERROR_CODE: ClassVar[str] = "STORAGE_NOT_FOUND"

    def __init__(
        self,
        message: str = "Object not found",
        *,
        store_type: str | None = None,
        path: str | None = None,
        cause: Exception | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(
            message,
            store_type=store_type,
            path=path,
            operation="get",
            cause=cause,
            error_code=error_code,
        )


class StoragePermissionError(StorageError):
    """Raised when access is denied."""

    DEFAULT_ERROR_CODE: ClassVar[str] = "STORAGE_PERMISSION"

    def __init__(
        self,
        message: str = "Access denied",
        *,
        store_type: str | None = None,
        path: str | None = None,
        operation: str | None = None,
        cause: Exception | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(
            message,
            store_type=store_type,
            path=path,
            operation=operation,
            cause=cause,
            error_code=error_code,
        )


class StorageConfigError(StorageError):
    """Raised when storage configuration is invalid.

    This includes DAPR binding parse errors and missing required fields.
    """

    DEFAULT_ERROR_CODE: ClassVar[str] = "STORAGE_CONFIG"

    def __init__(
        self,
        message: str,
        *,
        binding_name: str | None = None,
        config_file: str | None = None,
        cause: Exception | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message, cause=cause, error_code=error_code)
        self.binding_name = binding_name
        self.config_file = config_file

    def __str__(self) -> str:
        parts = [f"[{self.error_code}] {self.message}"]
        if self.binding_name:
            parts.append(f"binding={self.binding_name}")
        if self.config_file:
            parts.append(f"file={self.config_file}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


class StorageOperationError(StorageError):
    """Raised when a storage operation fails."""

    DEFAULT_ERROR_CODE: ClassVar[str] = "STORAGE_OPERATION"
