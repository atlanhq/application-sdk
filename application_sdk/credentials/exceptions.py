"""Custom exceptions for credential operations.

These exceptions provide specific error handling for different failure modes
in the credential system.
"""

from typing import List, Optional


class CredentialError(Exception):
    """Base exception for credential operations.

    All credential-related exceptions inherit from this class,
    allowing for broad exception handling when needed.
    """

    pass


class CredentialValidationError(CredentialError):
    """Raised when credential validation fails.

    Includes list of validation errors for detailed feedback.

    Attributes:
        message: Human-readable error message.
        errors: List of specific validation errors.

    Example:
        >>> raise CredentialValidationError(
        ...     "Validation failed",
        ...     errors=["api_key is required", "host must be a valid URL"]
        ... )
    """

    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or []


class CredentialResolutionError(CredentialError):
    """Raised when credential resolution fails.

    This can occur when:
    - Credential GUID not found
    - Failed to fetch from secret store
    - Invalid credential mapping

    Example:
        >>> raise CredentialResolutionError(
        ...     "Failed to resolve credential 'database': GUID not found"
        ... )
    """

    pass


class CredentialRefreshError(CredentialError):
    """Raised when token refresh fails.

    This can occur when:
    - Refresh token is invalid/expired
    - Token endpoint is unreachable
    - Authentication failed

    Example:
        >>> raise CredentialRefreshError(
        ...     "Failed to refresh OAuth token: refresh_token expired"
        ... )
    """

    pass


class CredentialNotFoundError(CredentialError):
    """Raised when a requested credential slot is not found.

    This occurs when code tries to access a credential slot
    that was not declared or not bound in the profile.

    Example:
        >>> raise CredentialNotFoundError(
        ...     "Credential slot 'stripe' not found. "
        ...     "Did you declare it in declare_credentials()?"
        ... )
    """

    pass


class ProtocolError(CredentialError):
    """Raised when a protocol operation fails.

    This can occur during:
    - apply() - Adding auth to request
    - materialize() - Formatting for SDK
    - refresh() - Refreshing tokens

    Example:
        >>> raise ProtocolError(
        ...     "Failed to apply AWS SigV4 signature: invalid region"
        ... )
    """

    pass


class CredentialHandleError(CredentialError):
    """Raised when attempting forbidden operations on CredentialHandle.

    The CredentialHandle blocks dangerous operations to prevent
    accidental credential exposure:
    - dict(handle)
    - print(handle)
    - json.dumps(handle)
    - iterating over handle

    Example:
        >>> handle = ctx.credentials.materialize("database")
        >>> dict(handle)  # Raises CredentialHandleError
        CredentialHandleError: Cannot convert CredentialHandle to dict.
        Use handle.get(field) instead.
    """

    pass


class CredentialBootstrapError(CredentialError):
    """Raised when credential bootstrapping fails at workflow start.

    This can occur when:
    - JWT validation fails
    - Heracles mapping endpoint is unreachable
    - Secret fetch from Vault fails

    Example:
        >>> raise CredentialBootstrapError(
        ...     "Failed to bootstrap credentials: JWT expired"
        ... )
    """

    pass
