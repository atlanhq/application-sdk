"""CredentialVault — runtime GUID-based credential resolution.

Protocol-based interface for resolving credentials from a secret vault
(e.g. HashiCorp Vault, Dapr secret store) by a runtime GUID.  The GUID
is a pointer to a credential record that may itself contain references to
secrets in a separate store.
"""

import warnings
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from application_sdk.errors import CREDENTIAL_VAULT_ERROR, ErrorCode
from application_sdk.errors.leaves import DependencyUnavailableError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class CredentialVaultError(DependencyUnavailableError):
    """Deprecated: use ``application_sdk.infrastructure.CredentialVaultUnavailableError`` — removed in v4.0."""

    #: Leaf instances never run this ``__init__``; the class default keeps the
    #: inherited ``error_code`` and ``__str__`` working on them.
    _error_code: ErrorCode | None = None
    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_VAULT_ERROR
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_CREDENTIAL_VAULT"

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
        if self.credential_guid:
            parts.append(f"credential_guid={self.credential_guid}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


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
