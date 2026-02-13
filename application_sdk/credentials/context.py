"""Credential context for workflow activities.

Provides ctx.http and ctx.credentials accessors for activities
to access authenticated HTTP clients and materialized credentials.
"""

import contextvars
from typing import Any, Dict, Optional

import httpx

from application_sdk.credentials.exceptions import (
    CredentialBootstrapError,
    CredentialNotFoundError,
)
from application_sdk.credentials.handle import CredentialHandle
from application_sdk.credentials.http_client import AuthenticatedHTTPClient
from application_sdk.credentials.store import WorkerCredentialStore
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class CredentialHTTPAccessor:
    """HTTP accessor for making authenticated requests.

    Provides a simple interface for making HTTP requests with
    automatic credential injection based on the slot name.

    The accessor supports two URL patterns:

    1. **Full URL** - Pass complete URL:
        >>> response = await ctx.http.get("stripe", "https://api.stripe.com/v1/charges")

    2. **Path only** - When base_url is configured in Credential declaration:
        >>> # Credential configured with base_url="https://api.stripe.com"
        >>> response = await ctx.http.get("stripe", "/v1/charges")

    Features:
        - Automatic authentication injection via configured protocol
        - Automatic token refresh on 401/403 (for TOKEN_EXCHANGE protocols)
        - Base URL support for cleaner API calls
        - All standard HTTP methods (GET, POST, PUT, PATCH, DELETE)

    Usage:
        >>> # In an activity
        >>> ctx = get_credential_context()
        >>>
        >>> # GET with query params
        >>> response = await ctx.http.get("api", "/users", params={"limit": 10})
        >>>
        >>> # POST with JSON body
        >>> response = await ctx.http.post("api", "/users", json={"name": "John"})
        >>>
        >>> # Custom headers (merged with auth headers)
        >>> response = await ctx.http.get("api", "/data",
        ...     headers={"X-Custom": "value"})

    All HTTP methods delegate to the underlying AuthenticatedHTTPClient
    which handles authentication injection via the protocol.
    """

    def __init__(self, workflow_id: str) -> None:
        """Initialize the HTTP accessor.

        Args:
            workflow_id: The workflow ID for credential lookup.
        """
        self._workflow_id = workflow_id
        self._store = WorkerCredentialStore.get_instance()

    def _get_client(self, slot_name: str) -> AuthenticatedHTTPClient:
        """Get the authenticated HTTP client for a slot.

        Args:
            slot_name: Name of the credential slot.

        Returns:
            AuthenticatedHTTPClient for the slot.

        Raises:
            CredentialNotFoundError: If no client exists for the slot.
        """
        client = self._store.get_http_client(self._workflow_id, slot_name)
        if client is None:
            raise CredentialNotFoundError(
                f"No HTTP client found for slot '{slot_name}'. "
                f"Ensure the credential is declared and bootstrapped."
            )
        return client

    async def get(
        self,
        slot_name: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated GET request.

        Args:
            slot_name: Name of the credential slot.
            url: Request URL.
            params: Optional query parameters.
            headers: Optional request headers.
            **kwargs: Additional request kwargs.

        Returns:
            HTTP response.
        """
        client = self._get_client(slot_name)
        return await client.get(url, params=params, headers=headers, **kwargs)

    async def post(
        self,
        slot_name: str,
        url: str,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated POST request.

        Args:
            slot_name: Name of the credential slot.
            url: Request URL.
            json: Optional JSON body.
            data: Optional form data.
            headers: Optional request headers.
            params: Optional query parameters.
            **kwargs: Additional request kwargs.

        Returns:
            HTTP response.
        """
        client = self._get_client(slot_name)
        return await client.post(
            url, json=json, data=data, headers=headers, params=params, **kwargs
        )

    async def put(
        self,
        slot_name: str,
        url: str,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated PUT request.

        Args:
            slot_name: Name of the credential slot.
            url: Request URL.
            json: Optional JSON body.
            data: Optional form data.
            headers: Optional request headers.
            params: Optional query parameters.
            **kwargs: Additional request kwargs.

        Returns:
            HTTP response.
        """
        client = self._get_client(slot_name)
        return await client.put(
            url, json=json, data=data, headers=headers, params=params, **kwargs
        )

    async def patch(
        self,
        slot_name: str,
        url: str,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated PATCH request.

        Args:
            slot_name: Name of the credential slot.
            url: Request URL.
            json: Optional JSON body.
            data: Optional form data.
            headers: Optional request headers.
            params: Optional query parameters.
            **kwargs: Additional request kwargs.

        Returns:
            HTTP response.
        """
        client = self._get_client(slot_name)
        return await client.patch(
            url, json=json, data=data, headers=headers, params=params, **kwargs
        )

    async def delete(
        self,
        slot_name: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated DELETE request.

        Args:
            slot_name: Name of the credential slot.
            url: Request URL.
            headers: Optional request headers.
            params: Optional query parameters.
            **kwargs: Additional request kwargs.

        Returns:
            HTTP response.
        """
        client = self._get_client(slot_name)
        return await client.delete(url, headers=headers, params=params, **kwargs)

    async def request(
        self,
        slot_name: str,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated request with any HTTP method.

        Args:
            slot_name: Name of the credential slot.
            method: HTTP method (GET, POST, etc.).
            url: Request URL.
            **kwargs: Request kwargs (headers, params, json, data, etc.).

        Returns:
            HTTP response.
        """
        client = self._get_client(slot_name)
        return await client.request(method, url, **kwargs)


class CredentialAccessor:
    """Accessor for materializing credentials.

    Provides access to credential handles for SDK initialization.
    Handles are opaque wrappers that prevent accidental exposure.

    Usage:
        >>> # In an activity
        >>> creds = await ctx.credentials.materialize("snowflake")
        >>> conn = snowflake.connector.connect(
        ...     account=creds.get("account"),
        ...     user=creds.get("user"),
        ...     password=creds.get("password"),
        ... )
    """

    def __init__(self, workflow_id: str) -> None:
        """Initialize the credential accessor.

        Args:
            workflow_id: The workflow ID for credential lookup.
        """
        self._workflow_id = workflow_id
        self._store = WorkerCredentialStore.get_instance()

    async def materialize(self, slot_name: str) -> CredentialHandle:
        """Materialize credentials for SDK usage.

        Returns an opaque CredentialHandle that provides safe access
        to credential values. The handle blocks dangerous operations
        like serialization, iteration, and printing.

        Args:
            slot_name: Name of the credential slot.

        Returns:
            CredentialHandle for safe credential access.

        Raises:
            CredentialNotFoundError: If no credential exists for the slot.

        Example:
            >>> handle = await ctx.credentials.materialize("database")
            >>> # Safe - individual field access
            >>> password = handle.get("password")
            >>> # Blocked - prevents accidental exposure
            >>> print(handle)  # Shows "<CredentialHandle [REDACTED]>"
            >>> dict(handle)   # Raises CredentialHandleError
        """
        handle = self._store.get_handle(self._workflow_id, slot_name)
        if handle is None:
            raise CredentialNotFoundError(
                f"No credential found for slot '{slot_name}'. "
                f"Ensure the credential is declared and bootstrapped."
            )

        logger.debug(
            "Credential materialized",
            extra={
                "workflow_id": self._workflow_id,
                "slot_name": slot_name,
            },
        )
        return handle

    def is_available(self, slot_name: str) -> bool:
        """Check if a credential slot is available.

        Args:
            slot_name: Name of the credential slot.

        Returns:
            True if the credential is available.
        """
        return self._store.get_handle(self._workflow_id, slot_name) is not None

    def get_available_slots(self) -> list[str]:
        """Get list of available credential slots.

        Returns:
            List of slot names that have credentials.
        """
        handles = self._store.get_all_handles(self._workflow_id)
        return list(handles.keys())


class CredentialContext:
    """Context object providing credential access to activities.

    This is the main interface for activities to access credentials.
    It provides two accessors:
    - ctx.http: For making authenticated HTTP requests
    - ctx.credentials: For materializing credentials for SDK usage

    The context is created by the CredentialInterceptor and stored
    in the WorkerCredentialStore. Activities access it via the
    activity context or a helper function.

    Example:
        >>> # In an activity
        >>> ctx = get_credential_context()
        >>>
        >>> # Make authenticated HTTP request
        >>> response = await ctx.http.get("stripe", "/v1/charges")
        >>>
        >>> # Or materialize for SDK
        >>> creds = await ctx.credentials.materialize("snowflake")
        >>> conn = snowflake.connector.connect(
        ...     account=creds.get("account"),
        ...     user=creds.get("user"),
        ...     password=creds.get("password"),
        ... )

    Attributes:
        workflow_id: The workflow ID this context belongs to.
        http: HTTP accessor for authenticated requests.
        credentials: Accessor for materializing credentials.
    """

    def __init__(self, workflow_id: str) -> None:
        """Initialize the credential context.

        Args:
            workflow_id: The workflow ID for this context.
        """
        self._workflow_id = workflow_id
        self._http = CredentialHTTPAccessor(workflow_id)
        self._credentials = CredentialAccessor(workflow_id)

    @property
    def workflow_id(self) -> str:
        """Get the workflow ID."""
        return self._workflow_id

    @property
    def http(self) -> CredentialHTTPAccessor:
        """Get the HTTP accessor for authenticated requests.

        Usage:
            >>> response = await ctx.http.get("api", "https://api.example.com/data")
        """
        return self._http

    @property
    def credentials(self) -> CredentialAccessor:
        """Get the credential accessor for SDK materialization.

        Usage:
            >>> handle = await ctx.credentials.materialize("database")
            >>> password = handle.get("password")
        """
        return self._credentials

    def is_initialized(self) -> bool:
        """Check if credentials have been bootstrapped.

        Returns:
            True if credentials are available.
        """
        store = WorkerCredentialStore.get_instance()
        return store.is_initialized(self._workflow_id)


# ============================================================================
# Context Access Helper
# ============================================================================

# Context variable for current workflow context (coroutine-safe)
_current_credential_context: contextvars.ContextVar[Optional[CredentialContext]] = (
    contextvars.ContextVar("credential_context", default=None)
)


def get_credential_context() -> CredentialContext:
    """Get the credential context for the current activity.

    This function retrieves the credential context that was set up
    by the CredentialInterceptor during workflow execution.

    Returns:
        CredentialContext for the current workflow.

    Raises:
        CredentialBootstrapError: If no context is available.

    Example:
        >>> from application_sdk.credentials import get_credential_context
        >>>
        >>> async def my_activity():
        ...     ctx = get_credential_context()
        ...     response = await ctx.http.get("api", "/endpoint")
    """
    ctx = _current_credential_context.get()
    if ctx is None:
        raise CredentialBootstrapError(
            "No credential context available. "
            "Ensure credentials are declared and the workflow is running "
            "with CredentialInterceptor enabled."
        )
    return ctx


def set_credential_context(ctx: Optional[CredentialContext]) -> None:
    """Set the credential context for the current execution.

    This is called by the CredentialInterceptor to set up the context
    before activity execution.

    Args:
        ctx: The credential context to set, or None to clear.
    """
    _current_credential_context.set(ctx)


def create_credential_context(workflow_id: str) -> CredentialContext:
    """Create a new credential context for a workflow.

    This is called by the CredentialInterceptor after bootstrapping
    credentials. The context provides access to:
    - ctx.http: Make authenticated HTTP requests
    - ctx.credentials: Materialize credentials for SDK usage

    Args:
        workflow_id: The workflow ID.

    Returns:
        CredentialContext: New CredentialContext instance with HTTP and
            credential accessors configured for the workflow.

    Example:
        >>> # Called internally by CredentialInterceptor
        >>> ctx = create_credential_context("workflow-123")
        >>> set_credential_context(ctx)
        >>>
        >>> # Then in activity code:
        >>> ctx = get_credential_context()
        >>> response = await ctx.http.get("api", "/endpoint")
    """
    return CredentialContext(workflow_id)
