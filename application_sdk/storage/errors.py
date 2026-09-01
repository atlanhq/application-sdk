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
    STORAGE_INTEGRITY,
    STORAGE_NOT_FOUND,
    STORAGE_OPERATION,
    STORAGE_PERMISSION,
    STORAGE_PREFLIGHT,
    STORAGE_RELOCATION,
    ErrorCode,
)
from application_sdk.errors.categories import Audience
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
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_STORAGE"

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
class StorageBucketRelocationError(StorageError):
    """A write was rejected because the bucket is being relocated.

    GCS rejects multipart upload *initiation* for the whole window of a
    dual-/multi-region bucket relocation (HTTP 400 ``PreconditionFailed``
    naming the relocation) while plain single-request PUTs keep working. The
    condition is temporary and entirely platform-side — no credential,
    permission, or connector change fixes it — so it carries its own code
    (the single definition; the preflight gate's storage check imports it as
    its block stamp) instead of the generic
    ``DEPENDENCY_UNAVAILABLE_STORAGE``, and a remediation hint saying to retry
    after the relocation finishes.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_RELOCATION
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION"

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        cause: Exception | None = None,
        suggested_action: str | None = None,
    ) -> None:
        StorageError.__init__(self, message, key=key, cause=cause)
        self.suggested_action = suggested_action


@dataclass(kw_only=True)
class StorageNotFoundError(NotFoundError, StorageError):
    """Object or key not found in the store.

    Categorical parent is ``NotFoundError`` (category=NOT_FOUND); domain
    parent is ``StorageError`` so ``except StorageError:`` still catches.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_NOT_FOUND
    code: ClassVar[str] = "NOT_FOUND_STORAGE"
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
    code: ClassVar[str] = "PERMISSION_STORAGE"
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
    code: ClassVar[str] = "INVALID_INPUT_STORAGE_CONFIG"
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

    code: ClassVar[str] = "INVALID_INPUT_STORAGE_BINDING_NOT_FOUND"
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

    code: ClassVar[str] = "INVALID_INPUT_STORAGE_BINDING_BROKEN"
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
    code: ClassVar[str] = "DATA_INTEGRITY_STORAGE_EMPTY_UPLOAD"
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


@dataclass(kw_only=True)
class StorageIntegrityError(DataIntegrityError, StorageError):
    """Transferred bytes do not match the digest recorded for them (FND-306).

    Raised by the transfer primitives when a file fails content validation:

    * **Download** — the downloaded bytes hash to something other than the
      ``{key}.sha256`` sidecar the producer wrote. Either the object in the
      store is corrupt at source (a producer that died mid-write, e.g. on
      ``ENOSPC``) or it was rewritten with content that no longer matches its
      sidecar.
    * **Upload** — the local file shrank while it was being read, so the bytes
      that landed in the store are a truncated prefix of the artifact the
      caller asked to upload.

    Non-retryable by design: a byte-stable corrupt input fails identically on
    every attempt, so burning the retry budget on it only delays the real
    signal and misattributes the failure to the consumer. Re-running the
    *producing* step is the remediation.

    Categorical parent is ``DataIntegrityError`` (category=DATA_INTEGRITY,
    audience=APP_OWNER, retryable=False); domain parent is ``StorageError``
    so ``except StorageError:`` catch blocks still fire.

    Attributes:
        key: Object-store key that failed validation.
        local_path: Local file the bytes were read from / written to.
        check: Which validation failed — ``"digest"`` (content does not match
            the recorded SHA-256) or ``"local_size"`` (source file shrank
            mid-upload).
        expectation: Digest the producer recorded, or the expected byte count.
        observed: Digest actually computed, or the observed byte count.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_INTEGRITY
    code: ClassVar[str] = "DATA_INTEGRITY_STORAGE_TRANSFER"
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.APP_OWNER

    local_path: str | None = None
    check: str | None = None

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        local_path: str | None = None,
        check: str | None = None,
        expectation: str | None = None,
        observed: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        DataIntegrityError.__init__(
            self,
            message=message,
            cause=cause,
            expectation=expectation,
            observed=observed,
            location=key,
        )
        self.key = key
        self.local_path = local_path
        self.check = check
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
        if self.local_path:
            parts.append(f"local_path={self.local_path}")
        if self.check:
            parts.append(f"check={self.check}")
        if self.expectation:
            parts.append(f"expected={self.expectation}")
        if self.observed:
            parts.append(f"observed={self.observed}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


@dataclass(kw_only=True)
class ObjectStorePreflightError(StorageError):
    """One or more object stores failed the SDR boot-time access preflight.

    Raised by ``application_sdk.storage.preflight.verify_object_store_access``
    when SDR mode is active (``ENABLE_ATLAN_UPLOAD=true``) and at least one
    configured store fails a write→read→delete round-trip probe, or when the
    upstream Atlan store is absent.

    The ``message`` contains a human-readable, per-store failure summary
    intended for an operator reading container logs.  ``failure_count`` is
    the number of individual store failures found.

    Because the failure may be due to config, credentials, or connectivity,
    the categorical parent is ``StorageError`` (``DependencyUnavailableError``)
    rather than any narrower leaf.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_PREFLIGHT
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_STORAGE_PREFLIGHT"
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.APP_OWNER

    failure_count: int = 0

    def __init__(
        self,
        message: str,
        *,
        failure_count: int = 0,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        StorageError.__init__(self, message=message, cause=cause, error_code=error_code)
        self.failure_count = failure_count

    def __str__(self) -> str:
        # The message already contains the full per-store report with newlines;
        # prepend the error code prefix for structured-log searchability.
        return f"[{self.error_code.code}] {self.message}"
