import asyncio
import warnings
from typing import Any, Literal, overload

import httpx
from httpx import Headers
from httpx._types import (
    AuthTypes,
    HeaderTypes,
    QueryParamTypes,
    RequestData,
    RequestFiles,
)

from application_sdk.clients._interface import ClientInterface
from application_sdk.clients.sql_errors import AbstractClientLoadError
from application_sdk.clients.ssl_utils import get_ssl_context
from application_sdk.constants import (
    _HTTP_CONNECT_RETRIES,
    _HTTP_POOL_LIMITS,
    _HTTP_POOL_TIMEOUT_SECONDS,
)
from application_sdk.errors.base import safe_traceback
from application_sdk.errors.http import classify_http_exception
from application_sdk.errors.leaves import SourceUnavailableError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class _SharedTransport(httpx.AsyncBaseTransport):
    """Lends a long-lived transport to a short-lived ``httpx.AsyncClient``.

    Each request builds its own ``AsyncClient``, and ``AsyncClient.__aexit__``
    closes its transport. When that transport is the client's shared pool,
    the first request to finish closes the sockets every concurrent request
    is still reading from, and they fail with ``httpx.ReadError``. This
    wrapper forwards requests and ignores the per-call enter and close; the
    owning ``BaseClient`` enters the real transport once, before its first
    request, and ``BaseClient.close()`` exits it.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)

    async def __aenter__(self) -> "_SharedTransport":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None


class BaseClient(ClientInterface):
    """
    Base client for non-SQL based applications.

    This class provides a base implementation for clients that need to connect
    to non-SQL data sources. It implements the ClientInterface and provides
    basic functionality that can be extended by subclasses.

    Attributes:
        credentials (Dict[str, Any]): Client credentials for authentication.
        http_headers (HeaderTypes): HTTP headers for all http requests made by this client. Supports dict, Headers object, or list of tuples.
        http_retry_transport (httpx.AsyncBaseTransport): HTTP transport for requests. Defaults to an
            httpx transport that retries connection setup up to 3 times. Can be overridden in load()
            for custom retry behavior.

    Extending the Client:
        To customize retry behavior, subclasses can override the http_retry_transport
        in the load() method, similar to how http_headers is set:

        Example:
            >>> class MyClient(BaseClient):
            ...     async def load(self, **kwargs):
            ...         # Set up HTTP headers in load method for better modularity
            ...         credentials = kwargs.get("credentials", {})
            ...         # Can use dict, Headers object, or list of tuples
            ...         self.http_headers = {
            ...             "Authorization": f"Bearer {credentials.get('token')}",
            ...             "User-Agent": "MyApp/1.0"
            ...         }
            ...         # Optionally override retry transport with custom configuration
            ...         # For advanced retry logic with status code handling, use httpx-retries:
            ...         # from httpx_retries import Retry, RetryTransport
            ...         # retry = Retry(total=5, backoff_factor=20)
            ...         # self.http_retry_transport = RetryTransport(retry=retry) #replace transport with custom transport if needed

        Advanced Retry Configuration:
            For applications requiring advanced retry logic (e.g., status code-based retries,
            rate limiting, custom backoff strategies), consider using httpx-retries library:

            >>> class MyClient(BaseClient):
            ...     async def load(self, **kwargs):
            ...         # Set up headers
            ...         self.http_headers = {"Authorization": f"Bearer {kwargs.get('token')}"}
            ...
            ...         # Install httpx-retries: pip install httpx-retries
            ...         from httpx_retries import Retry, RetryTransport
            ...
            ...         # Configure retry for status codes and network errors
            ...         retry = Retry(
            ...             total=5,
            ...             backoff_factor=10,
            ...             status_forcelist=[429, 500, 502, 503, 504]
            ...         )
            ...         self.http_retry_transport = RetryTransport(retry=retry)

        Header Management:
            The client supports a two-level header system using httpx Headers for merging headers:
            - Client-level headers: Set in the load() method and used for all requests
            - Method-level headers: Passed to individual methods and override/add to client headers

            Example:
                >>> client = MyClient()
                >>> await client.load(credentials={"token": "initial_token"})
                >>> # This request will use: {"Authorization": "Bearer initial_token", "User-Agent": "MyApp/1.0", "Content-Type": "application/json"}
                >>> response = await client.execute_http_post_request(
                ...     url="https://api.example.com/data",
                ...     headers={"Content-Type": "application/json"}
                ... )
    """

    def __init__(
        self,
        credentials: dict[str, Any] = {},
        http_headers: HeaderTypes = {},
    ):
        """
        Initialize the base client.

        Args:
            credentials (Dict[str, Any], optional): Client credentials for authentication. Defaults to {}.
            http_headers (HeaderTypes, optional): HTTP headers for all requests. Defaults to {}.
        """
        self.credentials = credentials
        self.http_headers = http_headers

        # Pool limits prevent CLOSE_WAIT zombie socket accumulation (see
        # BLDX-1153). httpcore retries only connection setup (ConnectError,
        # ConnectTimeout), before any request bytes are sent, so the retries
        # are safe for POST.
        self.http_retry_transport: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport(
            limits=_HTTP_POOL_LIMITS,
            retries=_HTTP_CONNECT_RETRIES,
        )
        # The transport entered by the first request and not yet exited by
        # close(). While set, its connection pool may hold sockets.
        self._entered_transport: httpx.AsyncBaseTransport | None = None
        self._enter_lock = asyncio.Lock()

    def __del__(self) -> None:
        """Warn when a client that made requests is collected unclosed.

        Guarded against interpreter teardown, when module globals such as
        ``warnings`` may already be cleared: the warning is best-effort and an
        exception from a finalizer can only surface as an unraisable error.
        """
        try:
            if getattr(self, "_entered_transport", None) is not None:
                warnings.warn(
                    f"{type(self).__name__} was not closed; its connection pool "
                    "may still hold sockets. Use 'async with' or call close(), "
                    "and call super().close() from a close() override.",
                    ResourceWarning,
                    source=self,
                    stacklevel=2,
                )
        except Exception:  # noqa: S110, BLE001 — teardown-only; see docstring
            pass

    async def _borrow_transport(self) -> _SharedTransport:
        """Enter ``http_retry_transport`` once, then lend it to a per-call client.

        A per-call ``AsyncClient`` used to enter and exit the transport on
        every request. The transport is now shared, so it is entered once here
        (again if ``load()`` swapped it) and exited by ``close()``.
        """
        transport = self.http_retry_transport
        if self._entered_transport is not transport:
            async with self._enter_lock:
                if self._entered_transport is not transport:
                    await transport.__aenter__()
                    self._entered_transport = transport
        return _SharedTransport(transport)

    async def load(self, **kwargs: Any) -> None:
        """
        Initialize the client with credentials and necessary attributes for the client to work.

        This method should be implemented by subclasses to:
        - Set up authentication headers in self.http_headers in case of http requestss
        - Initialize any required client state
        - Handle credential processing
        - Optionally override self.http_retry_transport for custom retry behavior

        For advanced retry logic (status code-based retries, rate limiting, custom backoff),
        consider using httpx-retries library and overriding http_retry_transport:

        Example:
            >>> async def load(self, **kwargs):
            ...     # Set up headers
            ...     self.http_headers = {"Authorization": f"Bearer {kwargs.get('token')}"}
            ...
            ...     # For advanced retry logic, install httpx-retries: pip install httpx-retries
            ...     from httpx_retries import Retry, RetryTransport
            ...     retry = Retry(total=5, backoff_factor=10, status_forcelist=[429, 500, 502, 503, 504])
            ...     self.http_retry_transport = RetryTransport(retry=retry)

        Args:
            **kwargs: Additional keyword arguments, typically including credentials.
                May also include retry configuration parameters that can be used to
                create a custom http_retry_transport.

        Raises:
            AbstractClientLoadError: If the subclass does not implement this method.
        """

        raise AbstractClientLoadError()

    async def close(self, *args: Any, **kwargs: Any) -> None:
        """Release the HTTP transport and its connection pool.

        Requests borrow ``http_retry_transport`` without closing it, so that
        concurrent requests on one client do not tear down each other's
        connections. Call this once the client is no longer needed, or use
        the client as an async context manager (``async with``), which calls it
        on exit. A subclass that overrides ``close()`` must call
        ``super().close()``, or the connection pool stays open.
        """
        entered, self._entered_transport = self._entered_transport, None
        if entered is not None:
            await entered.__aexit__(None, None, None)
        if entered is not self.http_retry_transport:
            await self.http_retry_transport.aclose()

    @overload
    async def execute_http_get_request(
        self,
        url: str,
        headers: HeaderTypes | None = None,
        params: QueryParamTypes | None = None,
        auth: AuthTypes | None = None,
        timeout: int = 10,
        *,
        raise_on_error: Literal[True],
    ) -> httpx.Response: ...

    @overload
    async def execute_http_get_request(
        self,
        url: str,
        headers: HeaderTypes | None = None,
        params: QueryParamTypes | None = None,
        auth: AuthTypes | None = None,
        timeout: int = 10,
        *,
        raise_on_error: bool | None = None,
    ) -> httpx.Response | None: ...

    async def execute_http_get_request(
        self,
        url: str,
        headers: HeaderTypes | None = None,
        params: QueryParamTypes | None = None,
        auth: AuthTypes | None = None,
        timeout: int = 10,
        *,
        raise_on_error: bool | None = None,
    ) -> httpx.Response | None:
        """
        Perform an HTTP GET request using the configured transport.

        The default transport retries connection setup (connect errors and
        connect timeouts) up to 3 times. It does not retry read errors, read
        timeouts or HTTP status codes, and any response, including 4xx and
        5xx, is returned as is. For status code-based retries (429, 500,
        etc.), override http_retry_transport in load(), for example with the
        httpx-retries library.

        Args:
            url (str): The URL to make the GET request to
            headers (Optional[HeaderTypes]): HTTP headers to include in the request. Supports dict, Headers object, or list of tuples. These headers will override/add to any client-level headers set in the load() method.
            params (Optional[QueryParamTypes]): Query parameters to include in the request. Supports dict, list of tuples, or string.
            auth (Optional[AuthTypes]): Authentication to use for the request. Supports BasicAuth, DigestAuth, custom auth classes, or tuples for basic auth.
            timeout (int): Request timeout in seconds. Defaults to 10.
            raise_on_error (Optional[bool]): When True, a request the source
                did not answer (network failure, timeout, dropped connection)
                raises SourceUnavailableError, and any other exception,
                including httpx.PoolTimeout, propagates unchanged.
                When False, every exception is logged and None is returned.
                Leaving it unset behaves like False and emits a
                DeprecationWarning: the default becomes True in v4.0.

        Returns:
            Optional[httpx.Response]: The HTTP response. None if the request
            failed and raise_on_error is not True.

        Raises:
            SourceUnavailableError: If raise_on_error is True and the request
                got no response. The httpx error is carried as ``cause``.

        Example:
            >>> # Using Basic Authentication
            >>> from httpx import BasicAuth
            >>> response = await client.execute_http_get_request(
            ...     url="https://api.example.com/data",
            ...     auth=BasicAuth("username", "password"),
            ...     params={"limit": 100},
            ...     raise_on_error=True,
            ... )
            >>>
            >>> # Using tuple for basic auth (username, password)
            >>> response = await client.execute_http_get_request(
            ...     url="https://api.example.com/data",
            ...     auth=("username", "password"),
            ...     params={"limit": 100},
            ...     raise_on_error=True,
            ... )
            >>>
            >>> # Using custom headers for Bearer token
            >>> response = await client.execute_http_get_request(
            ...     url="https://api.example.com/data",
            ...     headers={"Authorization": "Bearer token"},
            ...     params={"limit": 100},
            ...     raise_on_error=True,
            ... )
        """
        _warn_if_raise_on_error_unset("execute_http_get_request", raise_on_error)
        ssl_context = get_ssl_context()
        transport = await self._borrow_transport()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, pool=_HTTP_POOL_TIMEOUT_SECONDS),
            transport=transport,
            verify=ssl_context,
        ) as client:
            merged_headers = Headers(self.http_headers)
            if headers:
                merged_headers.update(headers)

            try:
                response = await client.get(
                    url,
                    headers=merged_headers,
                    params=params,
                    auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
                )
                return response
            except Exception as exc:
                return _request_failed("GET", url, exc, raise_on_error)

    @overload
    async def execute_http_post_request(
        self,
        url: str,
        data: RequestData | None = None,
        json_data: Any | None = None,
        content: bytes | None = None,
        files: RequestFiles | None = None,
        headers: HeaderTypes | None = None,
        params: QueryParamTypes | None = None,
        cookies: dict[str, str] | None = None,
        auth: AuthTypes | None = None,
        follow_redirects: bool = True,
        timeout: int = 30,
        *,
        raise_on_error: Literal[True],
    ) -> httpx.Response: ...

    @overload
    async def execute_http_post_request(
        self,
        url: str,
        data: RequestData | None = None,
        json_data: Any | None = None,
        content: bytes | None = None,
        files: RequestFiles | None = None,
        headers: HeaderTypes | None = None,
        params: QueryParamTypes | None = None,
        cookies: dict[str, str] | None = None,
        auth: AuthTypes | None = None,
        follow_redirects: bool = True,
        timeout: int = 30,
        *,
        raise_on_error: bool | None = None,
    ) -> httpx.Response | None: ...

    async def execute_http_post_request(
        self,
        url: str,
        data: RequestData | None = None,
        json_data: Any | None = None,
        content: bytes | None = None,
        files: RequestFiles | None = None,
        headers: HeaderTypes | None = None,
        params: QueryParamTypes | None = None,
        cookies: dict[str, str] | None = None,
        auth: AuthTypes | None = None,
        follow_redirects: bool = True,
        timeout: int = 30,
        *,
        raise_on_error: bool | None = None,
    ) -> httpx.Response | None:
        """
        Perform an HTTP POST request using the configured transport.

        The default transport retries connection setup (connect errors and
        connect timeouts) up to 3 times; no request bytes have been sent at
        that point, so the retry is safe for POST. It does not retry read
        errors, read timeouts or HTTP status codes, and any response,
        including 4xx and 5xx, is returned as is. For status code-based
        retries (429, 500, etc.), override http_retry_transport in load(),
        for example with the httpx-retries library.

        Args:
            url (str): The URL to make the POST request to
            data (Optional[RequestData]): Form data to send in the request body. Supports dict, list of tuples, or other httpx-compatible formats.
            json_data (Optional[Any]): JSON data to send in the request body. Any JSON-serializable object.
            content (Optional[bytes]): Raw binary content to send in the request body
            files (Optional[RequestFiles]): Files to upload in the request body. Supports various file formats and tuples.
            headers (Optional[HeaderTypes]): HTTP headers to include in the request. Supports dict, Headers object, or list of tuples. These headers will override/add to any client-level headers set in the load() method.
            params (Optional[QueryParamTypes]): Query parameters to include in the request. Supports dict, list of tuples, or string.
            cookies (Optional[Dict[str, str]]): Cookies to include in the request
            auth (Optional[AuthTypes]): Authentication to use for the request. Supports BasicAuth, DigestAuth, custom auth classes, or tuples for basic auth.
            follow_redirects (bool): Whether to follow HTTP redirects. Defaults to True.
            timeout (int): Request timeout in seconds. Defaults to 30.
            raise_on_error (Optional[bool]): When True, a request the source
                did not answer (network failure, timeout, dropped connection)
                raises SourceUnavailableError, and any other exception,
                including httpx.PoolTimeout, propagates unchanged.
                When False, every exception is logged and None is returned.
                Leaving it unset behaves like False and emits a
                DeprecationWarning: the default becomes True in v4.0.

        Returns:
            Optional[httpx.Response]: The HTTP response. None if the request
            failed and raise_on_error is not True.

        Raises:
            SourceUnavailableError: If raise_on_error is True and the request
                got no response. The httpx error is carried as ``cause``.

        Example:
            >>> # Basic JSON POST request with authentication
            >>> from httpx import BasicAuth
            >>> response = await client.execute_http_post_request(
            ...     url="https://api.example.com/data",
            ...     json_data={"name": "test", "value": 123},
            ...     headers={"Content-Type": "application/json"},
            ...     auth=BasicAuth("username", "password"),
            ...     raise_on_error=True,
            ... )
            >>>
            >>> # File upload with basic auth tuple
            >>> with open("file.txt", "rb") as f:
            ...     response = await client.execute_http_post_request(
            ...         url="https://api.example.com/upload",
            ...         data={"description": "My file"},
            ...         files={"file": ("file.txt", f.read(), "text/plain")},
            ...         auth=("username", "password"),
            ...         raise_on_error=True,
            ... )
        """
        _warn_if_raise_on_error_unset("execute_http_post_request", raise_on_error)
        ssl_context = get_ssl_context()
        transport = await self._borrow_transport()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, pool=_HTTP_POOL_TIMEOUT_SECONDS),
            transport=transport,
            verify=ssl_context,
        ) as client:
            merged_headers = Headers(self.http_headers)
            if headers:
                merged_headers.update(headers)

            try:
                response = await client.post(
                    url,
                    data=data,
                    json=json_data,
                    content=content,
                    files=files,
                    headers=merged_headers,
                    params=params,
                    cookies=cookies,
                    auth=auth if auth is not None else httpx.USE_CLIENT_DEFAULT,
                    follow_redirects=follow_redirects,
                )
                return response
            except Exception as exc:
                return _request_failed("POST", url, exc, raise_on_error)


def _warn_if_raise_on_error_unset(
    method_name: str, raise_on_error: bool | None
) -> None:
    """Warn a caller still relying on the deprecated ``None``-on-failure default."""
    if raise_on_error is None:
        warnings.warn(
            f"BaseClient.{method_name} returns None when a request fails unless "
            "raise_on_error=True is passed, which raises SourceUnavailableError "
            "instead. The default becomes raise_on_error=True in v4.0; pass "
            "raise_on_error=True (or False to keep None) explicitly.",
            DeprecationWarning,
            stacklevel=3,
        )


def _endpoint(url: str) -> str:
    """The request URL without userinfo, query or fragment, which can carry secrets."""
    try:
        return str(httpx.URL(url).copy_with(userinfo=b"", query=None, fragment=None))
    except httpx.InvalidURL:
        return "<invalid url>"


def _request_failed(
    method: str, url: str, exc: Exception, raise_on_error: bool | None
) -> None:
    """Handle an exception from an HTTP helper request.

    With ``raise_on_error``, a failure that :func:`classify_http_exception`
    attributes to the source (a network failure, a timeout, a dropped
    connection) becomes :class:`SourceUnavailableError`; anything else,
    including ``httpx.PoolTimeout``, an unsupported scheme and a redirect
    loop, is re-raised unchanged. Without it, the exception is logged and the
    helper returns ``None``.

    The log line carries a :func:`safe_traceback` with the request URL
    swapped for ``endpoint``, not ``exc_info``. A transport's error message
    can quote the full request URL, userinfo and query included, and the
    query may carry a secret under a name the redaction patterns do not
    know; ``exc_info`` would export it as is.
    """
    endpoint = _endpoint(url)
    if raise_on_error:
        if classify_http_exception(exc) is SourceUnavailableError:
            network_error = type(exc).__name__
            raise SourceUnavailableError(
                message=f"HTTP {method} {endpoint} failed: {network_error}",
                endpoint=endpoint,
                network_error=network_error,
                cause=exc,
            ) from exc
        raise exc
    logger.error(
        "Request failed for %s %s: %s",
        method,
        endpoint,
        safe_traceback(exc, replace=dict.fromkeys(_request_urls(url, exc), endpoint)),
    )
    return None


def _request_urls(url: str, exc: Exception) -> set[str]:
    """Every spelling of the request URL an exception message may quote.

    The caller's string, httpx's normalised form of it, and the sent
    request's URL, which also carries the merged ``params``.
    """
    urls = {url}
    try:
        urls.add(str(httpx.URL(url)))
    except httpx.InvalidURL:
        pass
    if isinstance(exc, httpx.RequestError):
        try:
            urls.add(str(exc.request.url))
        except RuntimeError:  # raised by httpx when no request was attached
            pass
    return urls
