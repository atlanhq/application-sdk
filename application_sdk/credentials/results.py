"""Result types for protocol methods.

These dataclasses are returned by protocol methods to provide structured
results for credential operations.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ApplyResult:
    """Result of applying credentials to a request.

    Contains modifications to be applied to the HTTP request.
    The HTTP client will merge these into the outgoing request.

    Attributes:
        headers: Headers to add/override in the request.
        query_params: Query parameters to add to the URL.
        body: Body modifications (for form-encoded or JSON bodies).
        auth: httpx AuthTypes object for special auth (e.g., client certs).

    Example:
        >>> # API Key in header
        >>> ApplyResult(headers={"Authorization": "Bearer sk_live_xxx"})
        >>>
        >>> # API Key in query param
        >>> ApplyResult(query_params={"api_key": "xxx"})
        >>>
        >>> # Basic Auth
        >>> ApplyResult(headers={"Authorization": "Basic dXNlcjpwYXNz"})
    """

    headers: Dict[str, str] = field(default_factory=dict)
    query_params: Dict[str, str] = field(default_factory=dict)
    body: Optional[Dict[str, Any]] = None
    auth: Optional[Any] = None  # For httpx AuthTypes (e.g., client certs)


@dataclass
class ValidationResult:
    """Result of credential validation.

    Returned by protocol.validate() to indicate whether credentials
    are valid and provide error/warning messages.

    Attributes:
        valid: Whether the credentials passed validation.
        errors: List of validation error messages (blocking).
        warnings: List of validation warning messages (non-blocking).

    Example:
        >>> # Valid credentials
        >>> ValidationResult(valid=True)
        >>>
        >>> # Missing required field
        >>> ValidationResult(
        ...     valid=False,
        ...     errors=["api_key is required"]
        ... )
        >>>
        >>> # Valid with warnings
        >>> ValidationResult(
        ...     valid=True,
        ...     warnings=["Token expires in 24 hours"]
        ... )
    """

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class MaterializeResult:
    """Result of materializing credentials for SDK usage.

    Returned by protocol.materialize() with SDK-ready credential values.

    Attributes:
        credentials: Dictionary of credentials formatted for SDK usage.
        expiry: Unix timestamp when credentials expire (optional).

    Example:
        >>> # AWS credentials for boto3
        >>> MaterializeResult(
        ...     credentials={
        ...         "aws_access_key_id": "AKIA...",
        ...         "aws_secret_access_key": "xxx",
        ...         "region_name": "us-east-1"
        ...     }
        ... )
        >>>
        >>> # OAuth token with expiry
        >>> MaterializeResult(
        ...     credentials={"access_token": "eyJ..."},
        ...     expiry=1699999999.0
        ... )
    """

    credentials: Dict[str, Any] = field(default_factory=dict)
    expiry: Optional[float] = None  # Unix timestamp when credentials expire
