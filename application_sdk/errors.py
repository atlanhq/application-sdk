"""Structured error codes for Application SDK.

Error code format: AAF-{COMP}-{ID:03d}
- AAF = Atlan App Framework prefix
- {COMP} = 3-letter component code
- {ID} = 3-digit zero-padded sequential number

Public API (cross-component-boundary codes apps catch or raise)::

    from application_sdk.errors import (
        ErrorCode,
        APP_ERROR, APP_NON_RETRYABLE,
        HANDLER_ERROR,
        CONTRACT_VALIDATION, PAYLOAD_SAFETY,
        CREDENTIAL_ERROR, CREDENTIAL_NOT_FOUND,
        STORAGE_NOT_FOUND, SECRET_NOT_FOUND,
    )
"""

from dataclasses import dataclass

__all__ = [
    "ErrorCode",
    # App
    "APP_ERROR",
    "APP_NON_RETRYABLE",
    # Handler
    "HANDLER_ERROR",
    # Contracts
    "CONTRACT_VALIDATION",
    "PAYLOAD_SAFETY",
    # Credentials
    "CREDENTIAL_ERROR",
    "CREDENTIAL_NOT_FOUND",
    # Storage / secrets
    "STORAGE_NOT_FOUND",
    "SECRET_NOT_FOUND",
]


@dataclass(frozen=True)
class ErrorCode:
    """Structured error code for monitoring and alerting."""

    component: str
    id: int

    @property
    def code(self) -> str:
        """Format as AAF-{COMP}-{ID:03d}."""
        return f"AAF-{self.component}-{self.id:03d}"

    def __str__(self) -> str:
        return self.code


# APP - Core App errors
APP_ERROR = ErrorCode("APP", 1)
APP_NON_RETRYABLE = ErrorCode("APP", 2)
APP_CONTEXT_ERROR = ErrorCode("APP", 3)
APP_NOT_FOUND = ErrorCode("APP", 4)
APP_ALREADY_REGISTERED = ErrorCode("APP", 5)
TASK_NOT_FOUND = ErrorCode("APP", 6)

# STR - Storage errors
STORAGE_NOT_FOUND = ErrorCode("STR", 1)
STORAGE_PERMISSION = ErrorCode("STR", 2)
STORAGE_CONFIG = ErrorCode("STR", 3)
STORAGE_OPERATION = ErrorCode("STR", 4)

# CTR - Contract errors
CONTRACT_VALIDATION = ErrorCode("CTR", 1)
PAYLOAD_SAFETY = ErrorCode("CTR", 2)

# HDL - Handler errors
HANDLER_ERROR = ErrorCode("HDL", 1)

# EXE - Execution errors
EXECUTION_ERROR = ErrorCode("EXE", 1)
EXECUTION_WORKER_ERROR = ErrorCode("EXE", 2)
EXECUTION_ACTIVITY_ERROR = ErrorCode("EXE", 3)
EXECUTION_WORKER_EVICTED = ErrorCode("EXE", 4)

# INF - Infrastructure errors
STATE_STORE_ERROR = ErrorCode("INF", 1)
PUBSUB_ERROR = ErrorCode("INF", 2)
BINDING_ERROR = ErrorCode("INF", 3)
SECRET_STORE_ERROR = ErrorCode("INF", 4)
SECRET_NOT_FOUND = ErrorCode("INF", 5)

# CRD - Credential errors
CREDENTIAL_ERROR = ErrorCode("CRD", 1)
CREDENTIAL_NOT_FOUND = ErrorCode("CRD", 2)
CREDENTIAL_PARSE_ERROR = ErrorCode("CRD", 3)
CREDENTIAL_VALIDATION_ERROR = ErrorCode("CRD", 4)
CREDENTIAL_VAULT_ERROR = ErrorCode("CRD", 5)

# DSC - Discovery errors
DISCOVERY_ERROR = ErrorCode("DSC", 1)

# EVT - Event/Analytics errors
EVENT_PUBLISH = ErrorCode("EVT", 1)
EVENT_BUS = ErrorCode("EVT", 2)
SEGMENT_ERROR = ErrorCode("EVT", 3)
