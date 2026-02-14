"""Authenticated HTTP client with automatic credential injection.

The AuthenticatedHTTPClient wraps httpx.AsyncClient and automatically
applies authentication using the configured protocol before each request.
"""

from typing import Any, Dict, Optional

import httpx

from application_sdk.credentials.exceptions import CredentialRefreshError, ProtocolError
from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.results import ApplyResult
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class AuthenticatedHTTPClient:
    """HTTP client that automatically injects authentication.

    Wraps httpx.AsyncClient with automatic authentication injection
    using the configured protocol. Supports automatic retry on 401/403
    with token refresh.

    Usage:
        >>> # Created via ctx.http accessor, not directly
        >>> response = await ctx.http.get("api_slot", "https://api.example.com/users")
        >>> data = response.json()

    Features:
        - Automatic auth injection via protocol.apply()
        - Automatic retry on 401/403 with token refresh
        - Full httpx.AsyncClient compatibility
        - Proper resource cleanup

    Attributes:
        slot_name: Name of the credential slot.
        protocol: Protocol instance for authentication.
        workflow_id: ID of the workflow using this client.
    """

    def __init__(
        self,
        slot_name: str,
        credentials: Dict[str, Any],
        protocol: BaseProtocol,
        workflow_id: str,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 1,
        **httpx_kwargs: Any,
    ) -> None:
        """Initialize the authenticated HTTP client.

        Args:
            slot_name: Name of the credential slot.
            credentials: Credential values for authentication.
            protocol: Protocol instance for authentication.
            workflow_id: ID of the workflow using this client.
            base_url: Optional base URL for all requests.
            timeout: Request timeout in seconds.
            max_retries: Max retries on auth failure (default: 1).
            **httpx_kwargs: Additional arguments for httpx.AsyncClient.
        """
        self._slot_name = slot_name
        self._credentials = credentials
        self._protocol = protocol
        self._workflow_id = workflow_id
        self._base_url = base_url
        self._timeout = timeout
        self._max_retries = max_retries
        self._httpx_kwargs = httpx_kwargs
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def slot_name(self) -> str:
        """Get the credential slot name."""
        return self._slot_name

    @property
    def workflow_id(self) -> str:
        """Get the workflow ID."""
        return self._workflow_id

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the underlying httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url or "",
                timeout=self._timeout,
                **self._httpx_kwargs,
            )
        return self._client

    def _apply_auth(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Apply authentication to request parameters.

        Args:
            method: HTTP method.
            url: Request URL.
            headers: Request headers.
            params: Query parameters.
            json: JSON body.
            data: Form data.
            **kwargs: Additional request kwargs.

        Returns:
            Modified request kwargs with auth applied.

        Raises:
            ProtocolError: If auth application fails.
        """
        request_info = {
            "method": method.upper(),
            "url": url,
            "headers": dict(headers) if headers else {},
            "params": dict(params) if params else {},
            "body": json or data,
        }

        try:
            result: ApplyResult = self._protocol.apply(self._credentials, request_info)
        except Exception as e:
            raise ProtocolError(
                f"Failed to apply auth for slot '{self._slot_name}': {e}"
            ) from e

        # Build final request kwargs
        final_headers = dict(headers) if headers else {}
        final_params = dict(params) if params else {}
        final_kwargs = dict(kwargs)

        # Apply headers from protocol
        if result.headers:
            final_headers.update(result.headers)

        # Apply query params from protocol
        if result.query_params:
            final_params.update(result.query_params)

        # Apply auth config (for certificate-based auth)
        if result.auth:
            if "cert" in result.auth:
                final_kwargs["cert"] = result.auth["cert"]
            if "verify" in result.auth:
                final_kwargs["verify"] = result.auth["verify"]

        return {
            "headers": final_headers,
            "params": final_params,
            "json": json,
            "data": data,
            **final_kwargs,
        }

    async def _refresh_and_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Refresh credentials and retry the request.

        Args:
            method: HTTP method.
            url: Request URL.
            **kwargs: Original request kwargs.

        Returns:
            HTTP response from retry.

        Raises:
            CredentialRefreshError: If refresh fails.
        """
        logger.debug(
            f"Refreshing credentials for slot '{self._slot_name}'",
            extra={
                "workflow_id": self._workflow_id,
                "slot_name": self._slot_name,
            },
        )

        try:
            self._credentials = self._protocol.refresh(self._credentials)
        except Exception as e:
            raise CredentialRefreshError(
                f"Failed to refresh credentials for slot '{self._slot_name}': {e}"
            ) from e

        # Update stored credentials in the store
        from application_sdk.credentials.store import WorkerCredentialStore

        store = WorkerCredentialStore.get_instance()
        store.set_raw_credentials(self._workflow_id, self._slot_name, self._credentials)

        # Re-apply auth and make request
        auth_kwargs = self._apply_auth(method, url, **kwargs)
        client = await self._get_client()
        return await client.request(method, url, **auth_kwargs)

    async def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        retry_on_auth_failure: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated HTTP request.

        Args:
            method: HTTP method (GET, POST, etc.).
            url: Request URL.
            headers: Optional request headers.
            params: Optional query parameters.
            json: Optional JSON body.
            data: Optional form data.
            retry_on_auth_failure: Retry on 401/403 after refresh.
            **kwargs: Additional httpx request kwargs.

        Returns:
            HTTP response.

        Raises:
            ProtocolError: If auth application fails.
            CredentialRefreshError: If refresh fails.
            httpx.HTTPError: On request failure.
        """
        auth_kwargs = self._apply_auth(
            method, url, headers, params, json, data, **kwargs
        )

        client = await self._get_client()
        response = await client.request(method, url, **auth_kwargs)

        # Check for auth failure and retry if enabled
        if (
            response.status_code in (401, 403)
            and retry_on_auth_failure
            and self._max_retries > 0
        ):
            logger.debug(
                f"Auth failure ({response.status_code}) for slot '{self._slot_name}', "
                f"attempting refresh",
                extra={
                    "workflow_id": self._workflow_id,
                    "slot_name": self._slot_name,
                    "status_code": response.status_code,
                },
            )
            return await self._refresh_and_retry(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
                data=data,
                **kwargs,
            )

        return response

    async def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated GET request.

        Args:
            url: Request URL.
            params: Optional query parameters.
            headers: Optional request headers.
            **kwargs: Additional request kwargs.

        Returns:
            HTTP response.
        """
        return await self.request("GET", url, headers=headers, params=params, **kwargs)

    async def post(
        self,
        url: str,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated POST request.

        Args:
            url: Request URL.
            json: Optional JSON body.
            data: Optional form data.
            headers: Optional request headers.
            params: Optional query parameters.
            **kwargs: Additional request kwargs.

        Returns:
            HTTP response.
        """
        return await self.request(
            "POST", url, headers=headers, params=params, json=json, data=data, **kwargs
        )

    async def put(
        self,
        url: str,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated PUT request.

        Args:
            url: Request URL.
            json: Optional JSON body.
            data: Optional form data.
            headers: Optional request headers.
            params: Optional query parameters.
            **kwargs: Additional request kwargs.

        Returns:
            HTTP response.
        """
        return await self.request(
            "PUT", url, headers=headers, params=params, json=json, data=data, **kwargs
        )

    async def patch(
        self,
        url: str,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated PATCH request.

        Args:
            url: Request URL.
            json: Optional JSON body.
            data: Optional form data.
            headers: Optional request headers.
            params: Optional query parameters.
            **kwargs: Additional request kwargs.

        Returns:
            HTTP response.
        """
        return await self.request(
            "PATCH", url, headers=headers, params=params, json=json, data=data, **kwargs
        )

    async def delete(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated DELETE request.

        Args:
            url: Request URL.
            headers: Optional request headers.
            params: Optional query parameters.
            **kwargs: Additional request kwargs.

        Returns:
            HTTP response.
        """
        return await self.request(
            "DELETE", url, headers=headers, params=params, **kwargs
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def close_sync(self) -> None:
        """Synchronously close the client (for cleanup)."""
        if self._client is not None and not self._client.is_closed:
            # httpx clients can be closed synchronously if not in async context
            try:
                import asyncio

                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Schedule close for later
                    loop.create_task(self._client.aclose())
                else:
                    loop.run_until_complete(self._client.aclose())
            except RuntimeError:
                # No event loop, force close
                pass
            finally:
                self._client = None

    async def __aenter__(self) -> "AuthenticatedHTTPClient":
        """Async context manager entry."""
        await self._get_client()
        return self

    async def __aexit__(
        self,
        exc_type: Any,
        exc_val: Any,
        exc_tb: Any,
    ) -> None:
        """Async context manager exit."""
        await self.close()
