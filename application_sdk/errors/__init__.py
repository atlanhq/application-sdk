"""Structured error codes and error hierarchy for Application SDK.

Canonical API (v3.x+)::

    from application_sdk.errors import (
        AppError,
        FailureCategory,
        Audience,
        FailureDetails,
        AuthError,
        AppPermissionDeniedError, AppTimeoutError,
        NotFoundError, AlreadyExistsError,
        InvalidInputError, PreconditionError,
        RateLimitedError, UnimplementedError,
        DependencyUnavailableError, SourceUnavailableError,
        ObjectStoreReadError, ObjectStoreDownloadError,
        ResourceExhaustedError, DiskFullError, SdrDiskFullError,
        DataIntegrityError, CancelledError, InternalError,
        classify_http_status, classify_http_exception,
    )

Legacy ``ErrorCode`` and the ``AAF-*`` constants (``APP_ERROR``,
``CREDENTIAL_NOT_FOUND``, ...) still import from here with a
``DeprecationWarning`` and are removed in v4.0.0. Each class's ``code``
ClassVar is the one error vocabulary.
"""

from typing import TYPE_CHECKING

from application_sdk import _deprecation
from application_sdk.errors import _legacy_codes

# Deprecated with @deprecated on the class: constructing one warns (FND-4299).
from application_sdk.errors._legacy_codes import ErrorCode

# ── New canonical hierarchy ──────────────────────────────────────────────────
from application_sdk.errors.base import (
    AppError,
    redact_secrets,
    redact_wire_value,
    safe_traceback,
    sanitize_cause_repr,
)
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.http import classify_http_exception, classify_http_status
from application_sdk.errors.leaves import (
    WORKER_EVICTED_TYPE,
    AlreadyExistsError,
    AppPermissionDeniedError,
    AppTimeoutError,
    AuthError,
    CancelledError,
    ColdStartRaceError,
    DaprSidecarUnreachableError,
    DataIntegrityError,
    DependencyUnavailableError,
    DiskFullError,
    InternalError,
    InvalidInputError,
    InvalidInputValueError,
    LocalVolumeUnwritableError,
    NotFoundError,
    ObjectStoreDownloadError,
    ObjectStoreReadError,
    PreconditionError,
    RateLimitedError,
    ResourceExhaustedError,
    SdrDiskFullError,
    SourceUnavailableError,
    SourceWarmupExhaustedError,
    TaskStalledError,
    UnimplementedError,
)
from application_sdk.errors.wire import FailureDetails

# ── Legacy ErrorCode + AAF-* constants (deprecated — removed v4.0.0) ────────
#
# The constants are served by a PEP 562 alias, so an app that reads one gets a
# DeprecationWarning naming its own line. ``ErrorCode`` itself is re-exported
# above and warns when constructed. The values live in
# ``errors._legacy_codes``, which the SDK imports directly and which does not
# warn. FND-4299.

#: name -> (replacement, why). Literal per entry: the surface gate and
#: gen-deprecations read this table statically.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "APP_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "APP_NON_RETRYABLE": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "APP_CONTEXT_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "APP_NOT_FOUND": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "APP_ALREADY_REGISTERED": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "TASK_NOT_FOUND": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_NOT_FOUND": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_PERMISSION": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_CONFIG": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_OPERATION": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_EMPTY_UPLOAD": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_PREFLIGHT": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_INTEGRITY": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_RELOCATION": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_HANDOFF_INCOMPLETE": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_GATEWAY_AUTH": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_DISK_FULL": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_GATEWAY_NO_ROUTE": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STORAGE_AUTH": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "CONTRACT_VALIDATION": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "PAYLOAD_SAFETY": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "HANDLER_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "EXECUTION_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "EXECUTION_WORKER_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "EXECUTION_ACTIVITY_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "STATE_STORE_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "PUBSUB_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "BINDING_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "SECRET_STORE_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "SECRET_NOT_FOUND": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "CREDENTIAL_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "CREDENTIAL_NOT_FOUND": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "CREDENTIAL_PARSE_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "CREDENTIAL_VALIDATION_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "CREDENTIAL_VAULT_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "DISCOVERY_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "EVENT_PUBLISH": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "EVENT_BUS": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
    "SEGMENT_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
}

if TYPE_CHECKING:
    # __getattr__ can only be typed -> object; this restores the real types for
    # type checkers without binding the names at runtime.
    from application_sdk.errors._legacy_codes import (
        APP_ALREADY_REGISTERED as APP_ALREADY_REGISTERED,
    )
    from application_sdk.errors._legacy_codes import (
        APP_CONTEXT_ERROR as APP_CONTEXT_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        APP_ERROR as APP_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        APP_NON_RETRYABLE as APP_NON_RETRYABLE,
    )
    from application_sdk.errors._legacy_codes import (
        APP_NOT_FOUND as APP_NOT_FOUND,
    )
    from application_sdk.errors._legacy_codes import (
        BINDING_ERROR as BINDING_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        CONTRACT_VALIDATION as CONTRACT_VALIDATION,
    )
    from application_sdk.errors._legacy_codes import (
        CREDENTIAL_ERROR as CREDENTIAL_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        CREDENTIAL_NOT_FOUND as CREDENTIAL_NOT_FOUND,
    )
    from application_sdk.errors._legacy_codes import (
        CREDENTIAL_PARSE_ERROR as CREDENTIAL_PARSE_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        CREDENTIAL_VALIDATION_ERROR as CREDENTIAL_VALIDATION_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        CREDENTIAL_VAULT_ERROR as CREDENTIAL_VAULT_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        DISCOVERY_ERROR as DISCOVERY_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        EVENT_BUS as EVENT_BUS,
    )
    from application_sdk.errors._legacy_codes import (
        EVENT_PUBLISH as EVENT_PUBLISH,
    )
    from application_sdk.errors._legacy_codes import (
        EXECUTION_ACTIVITY_ERROR as EXECUTION_ACTIVITY_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        EXECUTION_ERROR as EXECUTION_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        EXECUTION_WORKER_ERROR as EXECUTION_WORKER_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        HANDLER_ERROR as HANDLER_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        PAYLOAD_SAFETY as PAYLOAD_SAFETY,
    )
    from application_sdk.errors._legacy_codes import (
        PUBSUB_ERROR as PUBSUB_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        SECRET_NOT_FOUND as SECRET_NOT_FOUND,
    )
    from application_sdk.errors._legacy_codes import (
        SECRET_STORE_ERROR as SECRET_STORE_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        SEGMENT_ERROR as SEGMENT_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        STATE_STORE_ERROR as STATE_STORE_ERROR,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_AUTH as STORAGE_AUTH,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_CONFIG as STORAGE_CONFIG,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_DISK_FULL as STORAGE_DISK_FULL,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_EMPTY_UPLOAD as STORAGE_EMPTY_UPLOAD,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_GATEWAY_AUTH as STORAGE_GATEWAY_AUTH,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_GATEWAY_NO_ROUTE as STORAGE_GATEWAY_NO_ROUTE,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_HANDOFF_INCOMPLETE as STORAGE_HANDOFF_INCOMPLETE,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_INTEGRITY as STORAGE_INTEGRITY,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_NOT_FOUND as STORAGE_NOT_FOUND,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_OPERATION as STORAGE_OPERATION,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_PERMISSION as STORAGE_PERMISSION,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_PREFLIGHT as STORAGE_PREFLIGHT,
    )
    from application_sdk.errors._legacy_codes import (
        STORAGE_RELOCATION as STORAGE_RELOCATION,
    )
    from application_sdk.errors._legacy_codes import (
        TASK_NOT_FOUND as TASK_NOT_FOUND,
    )

__getattr__ = _deprecation.deprecated_module_getattr(
    __name__,
    _DEPRECATED_CONSTANTS,
    remove_in="4.0.0",
    resolve=lambda name: getattr(_legacy_codes, name),
)

__all__ = [
    # New canonical
    "AppError",
    "Audience",
    "FailureCategory",
    "FailureDetails",
    "AlreadyExistsError",
    "AppPermissionDeniedError",
    "AppTimeoutError",
    "AuthError",
    "CancelledError",
    "ColdStartRaceError",
    "DaprSidecarUnreachableError",
    "DataIntegrityError",
    "DependencyUnavailableError",
    "DiskFullError",
    "SdrDiskFullError",
    "LocalVolumeUnwritableError",
    "SourceUnavailableError",
    "SourceWarmupExhaustedError",
    "InternalError",
    "InvalidInputError",
    "InvalidInputValueError",
    "NotFoundError",
    "ObjectStoreDownloadError",
    "ObjectStoreReadError",
    "PreconditionError",
    "RateLimitedError",
    "ResourceExhaustedError",
    "TaskStalledError",
    "UnimplementedError",
    "WORKER_EVICTED_TYPE",
    "classify_http_exception",
    "classify_http_status",
    # Deprecated (@deprecated: constructing one warns) — removed in v4.0.0
    "ErrorCode",
    "redact_secrets",
    "redact_wire_value",
    "safe_traceback",
    "sanitize_cause_repr",
]
