"""Storage error classes.

Domain errors from the storage subsystem.  Each specialised class inherits
from the appropriate categorical leaf (first base, so ``category`` ClassVar
resolves there first) and from ``StorageError`` (second base, so
``except StorageError:`` domain-catch blocks keep working).

MRO convention: categorical leaf first, domain base second.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import (
    STORAGE_CONFIG,
    STORAGE_EMPTY_UPLOAD,
    STORAGE_NOT_FOUND,
    STORAGE_OPERATION,
    STORAGE_PERMISSION,
    ErrorCode,
)
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AppPermissionDeniedError,
    DataIntegrityError,
    DependencyUnavailableError,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
)


@dataclass(kw_only=True)
class StorageError(DependencyUnavailableError):
    """Generic storage-subsystem failure (category=DEPENDENCY_UNAVAILABLE).

    Use more specific subclasses when the failure mode is known.
    """

    key: str | None = None

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_OPERATION
    code: ClassVar[str] = "STORAGE"

    # Intentional: dataclass fields define the wire-evidence schema; custom __init__ preserves positional-message compat.
    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
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


@dataclass(kw_only=True)
class StorageNotFoundError(NotFoundError, StorageError):
    """Object or key not found in the store.

    Categorical parent is ``NotFoundError`` (category=NOT_FOUND); domain
    parent is ``StorageError`` so ``except StorageError:`` still catches.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_NOT_FOUND
    code: ClassVar[str] = "STORAGE_NOT_FOUND"
    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.USER

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
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


@dataclass(kw_only=True)
class StoragePermissionError(AppPermissionDeniedError, StorageError):
    """Bucket or object access denied.

    Categorical parent is ``AppPermissionDeniedError`` (category=PERMISSION);
    domain parent is ``StorageError``.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_PERMISSION
    code: ClassVar[str] = "STORAGE_PERMISSION"
    category: ClassVar[FailureCategory] = FailureCategory.PERMISSION
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.USER

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        AppPermissionDeniedError.__init__(self, message=message, cause=cause)
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


@dataclass(kw_only=True)
class StorageConfigError(InvalidInputError, StorageError):
    """Storage configuration is invalid (e.g., missing bucket name).

    Categorical parent is ``InvalidInputError`` (category=INVALID_INPUT);
    domain parent is ``StorageError``.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_CONFIG
    code: ClassVar[str] = "STORAGE_CONFIG"
    category: ClassVar[FailureCategory] = FailureCategory.INVALID_INPUT
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.USER

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
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


# code is the new structured identifier; legacy DEFAULT_ERROR_CODE inherited from
# StorageConfigError (AAF-STR-003, deprecated, kept for back-compat — do not override).
@dataclass(kw_only=True)
class StorageBindingNotFoundError(StorageConfigError):
    """No Dapr component with the given name exists in the components directory.

    Subclass of ``StorageConfigError`` so existing ``except StorageConfigError:``
    catch blocks keep working.  Use this type specifically to distinguish
    "component absent" from other configuration errors (e.g. wrong binding type).
    """

    code: ClassVar[str] = "STORAGE_BINDING_NOT_FOUND"
    binding_name: str | None = None

    def __init__(
        self,
        message: str,
        *,
        binding_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        StorageConfigError.__init__(
            self, message=message, cause=cause, error_code=error_code
        )
        self.binding_name = binding_name

    def __str__(self) -> str:
        parts = [f"[{self.error_code.code}] {self.message}"]
        if self.binding_name:
            parts.append(f"binding_name={self.binding_name}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


@dataclass(kw_only=True)
class StorageBindingBrokenError(StorageConfigError):
    """Dapr component YAML exists but has unresolvable configuration.

    Raised when a component is found but contains template placeholders
    (e.g. ``{{tenant}}``) or ``secretKeyRef`` entries whose env vars are
    absent.  Subclass of ``StorageConfigError`` so ``except StorageConfigError:``
    catch blocks keep working.  Distinct from ``StorageBindingNotFoundError``
    (component absent) so callers can treat "broken but present" as "absent"
    in optional contexts.
    """

    code: ClassVar[str] = "STORAGE_BINDING_BROKEN"
    binding_name: str | None = None
    broken_fields: list[str] | None = None

    def __init__(
        self,
        message: str,
        *,
        binding_name: str | None = None,
        broken_fields: list[str] | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        StorageConfigError.__init__(
            self, message=message, cause=cause, error_code=error_code
        )
        self.binding_name = binding_name
        self.broken_fields = broken_fields or []

    def __str__(self) -> str:
        parts = [f"[{self.error_code.code}] {self.message}"]
        if self.binding_name:
            parts.append(f"binding_name={self.binding_name}")
        if self.broken_fields:
            parts.append(f"broken_fields={', '.join(self.broken_fields)}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


@dataclass(kw_only=True)
class UnsafeUploadPathError(InvalidInputError):
    """Upload path is blocked — sensitive path, traversal, or user-defined block list."""

    code: ClassVar[str] = "INVALID_INPUT_UPLOAD_PATH_UNSAFE"
    message: str = "Upload path blocked"
    field: str | None = "path"
    unsafe_path: str | None = None


@dataclass(kw_only=True)
class ObjectStoreNotProvidedError(PreconditionError):
    """No object store is available — must pass store= or configure infrastructure."""

    code: ClassVar[str] = "PRECONDITION_OBJECT_STORE_NOT_PROVIDED"
    message: str = (
        "No ObjectStore provided and no infrastructure storage is configured. "
        "Pass store= explicitly or call set_infrastructure() with a storage store."
    )
    resource: str | None = "object_store"


@dataclass(kw_only=True)
class StorageEmptyUploadError(DataIntegrityError, StorageError):
    """Directory upload found zero files when raise_on_empty=True.

    Categorical parent is ``DataIntegrityError`` (category=DATA_INTEGRITY,
    audience=APP_OWNER, retryable=False); domain parent is ``StorageError``
    so ``except StorageError:`` catch blocks still fire.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_EMPTY_UPLOAD
    code: ClassVar[str] = "STORAGE_EMPTY_UPLOAD"
    category: ClassVar[FailureCategory] = FailureCategory.DATA_INTEGRITY
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.APP_OWNER

    local_path: str | None = None

    def __init__(
        self,
        message: str,
        *,
        local_path: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        DataIntegrityError.__init__(self, message=message, cause=cause)
        self.local_path = local_path
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
        if self.local_path:
            parts.append(f"local_path={self.local_path}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)
