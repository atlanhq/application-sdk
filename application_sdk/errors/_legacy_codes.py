"""The legacy ``AAF-{COMP}-{ID}`` error codes (deprecated, removed in v4.0.0).

The SDK's one error vocabulary is the ``code`` ClassVar on each
:class:`~application_sdk.errors.AppError` subclass. ``AAF-*`` codes are the
older vocabulary. They survive only for back-compat: the ``[AAF-…]`` prefix of
``str()`` on a legacy-coded error, ``Cls.DEFAULT_ERROR_CODE``, ``err.error_code``
and the ``error_code=`` constructor keyword.

Which class carries which ``AAF-*`` code is recorded once, in
:data:`_CLASS_CODES`. Classes no longer declare ``DEFAULT_ERROR_CODE`` beside
``code`` (FND-4299).

``ErrorCode`` is ``@deprecated``: constructing one warns, and type checkers flag
its use. ``application_sdk.errors`` re-exports it and serves the constants
through a PEP 562 alias that warns on access. The SDK imports the constants
from here, which does not warn. No SDK imports beyond ``_deprecation`` (standard
library and ``typing_extensions``), so ``errors.base`` can import it without a
cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from application_sdk._deprecation import deprecated


@deprecated(
    "ErrorCode is deprecated; use the code ClassVar of a typed "
    "application_sdk.errors.AppError subclass instead — AAF-* codes are a "
    "second error vocabulary beside code. Will be removed in v4.0.0."
)
@dataclass(frozen=True)
class ErrorCode:
    """Structured error code for monitoring and alerting.

    Deprecated: use ``FailureCategory`` and the ``code`` ClassVar of a typed
    ``AppError`` subclass. Removed in v4.0.0.
    """

    component: str
    id: int

    @property
    def code(self) -> str:
        """Format as AAF-{COMP}-{ID:03d}."""
        return f"AAF-{self.component}-{self.id:03d}"

    def __str__(self) -> str:
        return self.code


def _code(component: str, id: int) -> ErrorCode:
    """Build one of the module's constants without the construction warning.

    ``object.__new__`` skips the ``__new__`` that ``@deprecated`` installs, so
    importing this module never warns; an app that builds its own ``ErrorCode``
    still does.
    """
    code = object.__new__(ErrorCode)
    code.__init__(component, id)
    return code


# APP - Core App errors
APP_ERROR = _code("APP", 1)
APP_NON_RETRYABLE = _code("APP", 2)
APP_CONTEXT_ERROR = _code("APP", 3)
APP_NOT_FOUND = _code("APP", 4)
APP_ALREADY_REGISTERED = _code("APP", 5)
TASK_NOT_FOUND = _code("APP", 6)

# STR - Storage errors
STORAGE_NOT_FOUND = _code("STR", 1)
STORAGE_PERMISSION = _code("STR", 2)
STORAGE_CONFIG = _code("STR", 3)
STORAGE_OPERATION = _code("STR", 4)
STORAGE_EMPTY_UPLOAD = _code("STR", 5)
STORAGE_PREFLIGHT = _code("STR", 6)
STORAGE_INTEGRITY = _code("STR", 7)
STORAGE_RELOCATION = _code("STR", 8)
STORAGE_HANDOFF_INCOMPLETE = _code("STR", 9)
STORAGE_GATEWAY_AUTH = _code("STR", 10)
STORAGE_DISK_FULL = _code("STR", 11)
STORAGE_GATEWAY_NO_ROUTE = _code("STR", 12)
STORAGE_AUTH = _code("STR", 13)

# CTR - Contract errors
CONTRACT_VALIDATION = _code("CTR", 1)
PAYLOAD_SAFETY = _code("CTR", 2)

# HDL - Handler errors
HANDLER_ERROR = _code("HDL", 1)

# EXE - Execution errors
EXECUTION_ERROR = _code("EXE", 1)
EXECUTION_WORKER_ERROR = _code("EXE", 2)
EXECUTION_ACTIVITY_ERROR = _code("EXE", 3)

# INF - Infrastructure errors
STATE_STORE_ERROR = _code("INF", 1)
PUBSUB_ERROR = _code("INF", 2)
BINDING_ERROR = _code("INF", 3)
SECRET_STORE_ERROR = _code("INF", 4)
SECRET_NOT_FOUND = _code("INF", 5)

# CRD - Credential errors
CREDENTIAL_ERROR = _code("CRD", 1)
CREDENTIAL_NOT_FOUND = _code("CRD", 2)
CREDENTIAL_PARSE_ERROR = _code("CRD", 3)
CREDENTIAL_VALIDATION_ERROR = _code("CRD", 4)
CREDENTIAL_VAULT_ERROR = _code("CRD", 5)

# DSC - Discovery errors
DISCOVERY_ERROR = _code("DSC", 1)

# EVT - Event/Analytics errors
EVENT_PUBLISH = _code("EVT", 1)
EVENT_BUS = _code("EVT", 2)
SEGMENT_ERROR = _code("EVT", 3)

#: Every SDK class that carries an ``AAF-*`` code, keyed by
#: ``module.qualname``. A subclass inherits the nearest entry in its MRO, so
#: only the class that introduces a code is listed. Strings, not classes: the
#: classes live in modules that import this one.
_CLASS_CODES: dict[str, ErrorCode] = {
    "application_sdk._context_errors.AppContextError": APP_CONTEXT_ERROR,
    "application_sdk.app.base.AppError": APP_ERROR,
    "application_sdk.app.base.NonRetryableError": APP_NON_RETRYABLE,
    "application_sdk.app.base.RetryableError": APP_ERROR,
    "application_sdk.app.entrypoint.EntryPointContractError": CONTRACT_VALIDATION,
    "application_sdk.app.registry.AppAlreadyRegisteredError": APP_ALREADY_REGISTERED,
    "application_sdk.app.registry.AppNotFoundError": APP_NOT_FOUND,
    "application_sdk.app.registry.TaskNotFoundError": TASK_NOT_FOUND,
    "application_sdk.app.task.TaskContractError": CONTRACT_VALIDATION,
    "application_sdk.contracts.base.ContractValidationError": CONTRACT_VALIDATION,
    "application_sdk.contracts.base.PayloadSafetyError": PAYLOAD_SAFETY,
    "application_sdk.credentials.errors.CredentialError": CREDENTIAL_ERROR,
    "application_sdk.credentials.errors.CredentialNotFoundError": CREDENTIAL_NOT_FOUND,
    "application_sdk.credentials.errors.CredentialParseError": CREDENTIAL_PARSE_ERROR,
    "application_sdk.credentials.errors.CredentialValidationError": CREDENTIAL_VALIDATION_ERROR,
    "application_sdk.discovery.DiscoveryError": DISCOVERY_ERROR,
    "application_sdk.handler.base.HandlerError": HANDLER_ERROR,
    "application_sdk.infrastructure.bindings.BindingError": BINDING_ERROR,
    "application_sdk.infrastructure.credential_vault.CredentialVaultError": CREDENTIAL_VAULT_ERROR,
    "application_sdk.infrastructure.pubsub.PubSubError": PUBSUB_ERROR,
    "application_sdk.infrastructure.secrets.SecretNotFoundError": SECRET_NOT_FOUND,
    "application_sdk.infrastructure.secrets.SecretStoreError": SECRET_STORE_ERROR,
    "application_sdk.infrastructure.state.StateStoreError": STATE_STORE_ERROR,
    "application_sdk.storage.errors.ObjectStorePreflightError": STORAGE_PREFLIGHT,
    "application_sdk.storage.errors.StorageAuthError": STORAGE_AUTH,
    "application_sdk.storage.errors.StorageBucketRelocationError": STORAGE_RELOCATION,
    "application_sdk.storage.errors.StorageConfigError": STORAGE_CONFIG,
    "application_sdk.storage.errors.StorageDiskFullError": STORAGE_DISK_FULL,
    "application_sdk.storage.errors.StorageEmptyUploadError": STORAGE_EMPTY_UPLOAD,
    "application_sdk.storage.errors.StorageError": STORAGE_OPERATION,
    "application_sdk.storage.errors.StorageGatewayAuthUnavailableError": STORAGE_GATEWAY_AUTH,
    "application_sdk.storage.errors.StorageGatewayNoRouteError": STORAGE_GATEWAY_NO_ROUTE,
    "application_sdk.storage.errors.StorageHandoffIncompleteError": STORAGE_HANDOFF_INCOMPLETE,
    "application_sdk.storage.errors.StorageIntegrityError": STORAGE_INTEGRITY,
    "application_sdk.storage.errors.StorageNotFoundError": STORAGE_NOT_FOUND,
    "application_sdk.storage.errors.StoragePermissionError": STORAGE_PERMISSION,
    "application_sdk.storage.errors.UnplaceableDeclaredFileError": STORAGE_OPERATION,
}


def legacy_code_for(cls: type) -> ErrorCode | None:
    """The ``AAF-*`` code *cls* carries, or ``None`` when it carries none.

    Walks the MRO. A class body that binds its own ``DEFAULT_ERROR_CODE`` to an
    ``ErrorCode`` (an app subclass written before FND-4299) wins over the table,
    so that subclass keeps the code it chose.
    """
    for klass in cls.__mro__:
        own = klass.__dict__.get("DEFAULT_ERROR_CODE")
        if isinstance(own, ErrorCode):
            return own
        code = _CLASS_CODES.get(f"{klass.__module__}.{klass.__qualname__}")
        if code is not None:
            return code
    return None
