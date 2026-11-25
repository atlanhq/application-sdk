"""CredentialVault — runtime GUID-based credential resolution.

Protocol-based interface for resolving credentials from a secret vault
(e.g. HashiCorp Vault, Dapr secret store) by a runtime GUID.  The GUID
is a pointer to a credential record that may itself contain references to
secrets in a separate store.
"""

import warnings
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from application_sdk import _deprecation
from application_sdk.errors import _legacy_codes
from application_sdk.errors._legacy_codes import ErrorCode
from application_sdk.errors.base import _LegacyErrorCodeMixin
from application_sdk.errors.leaves import DependencyUnavailableError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@dataclass(kw_only=True)
class CredentialVaultError(_LegacyErrorCodeMixin, DependencyUnavailableError):
    """Deprecated: use ``application_sdk.infrastructure.CredentialVaultUnavailableError`` — removed in v4.0."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_CREDENTIAL_VAULT"
    _STR_FIELDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("credential_guid", "credential_guid"),
    )
    credential_guid: str | None = None

    def __init__(
        self,
        message: str,
        *,
        credential_guid: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "CredentialVaultError is deprecated; use application_sdk.infrastructure.CredentialVaultUnavailableError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        DependencyUnavailableError.__init__(self, message=message, cause=cause)
        self.credential_guid = credential_guid
        self._set_legacy_error_code(error_code)


@dataclass(kw_only=True)
class CredentialVaultUnavailableError(CredentialVaultError):
    """Resolving a credential by GUID failed.

    Raised by the SDK's own ``CredentialVault`` implementations. It subclasses the deprecated
    ``CredentialVaultError`` only until v4.0, so ``except CredentialVaultError:`` clauses keep matching
    what the SDK raises; constructing it does not warn. v4.0 deletes
    ``CredentialVaultError`` and re-bases this class onto ``DependencyUnavailableError``.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_CREDENTIAL_VAULT"
    credential_guid: str | None = None


@dataclass(kw_only=True)
class CredentialConfigNotFoundError(CredentialVaultUnavailableError):
    """The object store confirmed absence after the config re-read ladder.

    Keeps the vault's existing exception contract while distinguishing a missing
    config from transport failures for callers that enforce credential readiness.
    The code deliberately repeats the parent's: fleet consumers key on
    ``DEPENDENCY_UNAVAILABLE_CREDENTIAL_VAULT``, so only ``isinstance`` callers
    (the preflight gate) see the distinction.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_CREDENTIAL_VAULT"


@runtime_checkable
class CredentialVault(Protocol):
    """Protocol for GUID-based runtime credential resolution.

    Implementations retrieve a credential config by GUID and resolve any
    secret references contained within it.  The underlying backend
    (Dapr, HashiCorp Vault, etc.) is an implementation detail.
    """

    async def get_credentials(self, credential_guid: str) -> dict[str, Any]:
        """Resolve the full credential dict for *credential_guid*.

        Args:
            credential_guid: Unique identifier of the credential record.

        Returns:
            Dict containing the fully-resolved credential fields.

        Raises:
            CredentialVaultUnavailableError: If resolution fails.
        """
        ...


#: The AAF-* constants this module imported before FND-4299, kept importable
#: from here with a warning until v4.0.0. Literal per entry: the surface gate
#: and gen-deprecations read this table statically.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "CREDENTIAL_VAULT_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
}

__getattr__ = _deprecation.deprecated_module_getattr(
    __name__,
    _DEPRECATED_CONSTANTS,
    remove_in="4.0.0",
    resolve=lambda name: getattr(_legacy_codes, name),
)
