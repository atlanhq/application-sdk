"""CredentialVault — runtime GUID-based credential resolution.

Protocol-based interface for resolving credentials from a secret vault
(e.g. HashiCorp Vault, Dapr secret store) by a runtime GUID.  The GUID
is a pointer to a credential record that may itself contain references to
secrets in a separate store.
"""

from typing import Any, ClassVar, Protocol, runtime_checkable

from application_sdk.errors import CREDENTIAL_VAULT_ERROR, ErrorCode
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class CredentialVaultError(Exception):
    """Raised when credential vault operations fail."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_VAULT_ERROR

    def __init__(
        self,
        message: str,
        *,
        credential_guid: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.credential_guid = credential_guid
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
        if self.credential_guid:
            parts.append(f"credential_guid={self.credential_guid}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


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
            CredentialVaultError: If resolution fails.
        """
        ...
