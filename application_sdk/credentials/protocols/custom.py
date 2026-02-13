"""Custom protocol base class for escape hatch scenarios.

When none of the 6 standard protocols fit your use case,
extend CustomProtocol to implement your own authentication logic.
"""

from typing import Any, Dict, List

from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.results import (
    ApplyResult,
    MaterializeResult,
    ValidationResult,
)
from application_sdk.credentials.types import FieldSpec


class CustomProtocol(BaseProtocol):
    """Base class for fully custom authentication protocols.

    Use this when none of the 6 standard protocols fit your use case.
    The framework still handles:
    - Storage and caching
    - Context propagation
    - Lifecycle management
    - Integration with ctx.http and ctx.credentials.materialize()

    You implement the authentication logic.

    Example:
        >>> class MyBankingAuth(CustomProtocol):
        ...     default_fields = [
        ...         FieldSpec(name="merchant_id", required=True),
        ...         FieldSpec(name="api_key", sensitive=True, required=True),
        ...     ]
        ...
        ...     def apply(self, credentials, request_info):
        ...         # Custom multi-step authentication
        ...         session = self._get_session(credentials)
        ...         signature = self._compute_signature(request_info, session)
        ...         return ApplyResult(headers={
        ...             "X-Session-Id": session,
        ...             "X-Signature": signature,
        ...         })
        ...
        ...     def materialize(self, credentials):
        ...         return MaterializeResult(credentials={
        ...             "session": self._get_session(credentials)
        ...         })
        ...
        ...     def refresh(self, credentials):
        ...         # Custom session refresh logic
        ...         return self._refresh_session(credentials)
        ...
        ...     def _get_session(self, credentials):
        ...         # Your custom session logic
        ...         pass

    Then use it:
        >>> Credential(
        ...     name="banking_api",
        ...     protocol=MyBankingAuth(),
        ...     # fields are defined in the protocol
        ... )
    """

    default_fields: List[FieldSpec] = []
    default_config: Dict[str, Any] = {}

    def apply(
        self, credentials: Dict[str, Any], request_info: Dict[str, Any]
    ) -> ApplyResult:
        """Override this method to implement custom request authentication.

        Default implementation returns empty result (no auth applied).

        Args:
            credentials: Resolved credential values.
            request_info: Request context (method, url, headers, body).

        Returns:
            ApplyResult with headers, query_params, body modifications.
        """
        return ApplyResult()

    def materialize(self, credentials: Dict[str, Any]) -> MaterializeResult:
        """Override this method to implement custom SDK credential formatting.

        Default implementation returns credentials as-is.

        Args:
            credentials: Resolved credential values.

        Returns:
            MaterializeResult with SDK-compatible credentials.
        """
        return MaterializeResult(credentials=credentials)

    def refresh(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """Override this method to implement custom token refresh.

        Default implementation returns credentials unchanged.

        Args:
            credentials: Current credential values.

        Returns:
            Updated credentials dict.
        """
        return credentials

    def validate(self, credentials: Dict[str, Any]) -> ValidationResult:
        """Override this method to implement custom validation.

        Default implementation validates based on default_fields.

        Args:
            credentials: Credential values to validate.

        Returns:
            ValidationResult with valid flag and errors/warnings.
        """
        return super().validate(credentials)
