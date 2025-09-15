from typing import Any, Dict, Optional

import httpx
from httpx import Headers
from httpx._types import (
    AuthTypes,
    HeaderTypes,
    QueryParamTypes,
    RequestData,
    RequestFiles,
)

from application_sdk.clients import ClientInterface
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class BaseClient(ClientInterface):
    """
    Base client for non-SQL based applications.

    This class provides a base implementation for clients that need to connect
    to non-SQL data sources. It implements the ClientInterface and provides
    basic functionality that can be extended by subclasses.

    Attributes:
        credentials (Dict[str, Any]): Client credentials for authentication.
        http_headers (HeaderTypes): HTTP headers for all http requests made by this client. Supports dict, Headers object, or list of tuples.
        http_retry_transporter (httpx.AsyncBaseTransport): HTTP transport for requests. Uses httpx default transport by default.
            Can be overridden in load() method for custom retry behavior.

    Extending the Client:
        To customize retry behavior, subclasses can override the http_retry_transporter
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
            ...         # self.http_retry_transporter = RetryTransport(retry=retry) #replace transport with custom transport if needed

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
            ...         self.http_retry_transporter = RetryTransport(retry=retry)

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
        credentials: Dict[str, Any] = {},
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

        # Use httpx default transport (no retries on status codes)
        self.http_retry_transport: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport()

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
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("load method is not implemented")

    async def execute_http_get_request(
        self,
        url: str,
        headers: Optional[HeaderTypes] = None,
        params: Optional[QueryParamTypes] = None,
        auth: Optional[AuthTypes] = None,
        timeout: int = 10,
    ) -> Optional[httpx.Response]:
        """
        Perform an HTTP GET request using the configured transport.

        This method uses httpx default transport which only retries on network-level errors
        (connection failures, timeouts). For status code-based retries (429, 500, etc.),
        consider overriding http_retry_transport in the load() method using httpx-retries library.

        Args:
            url (str): The URL to make the GET request to
            headers (Optional[HeaderTypes]): HTTP headers to include in the request. Supports dict, Headers object, or list of tuples. These headers will override/add to any client-level headers set in the load() method.
            params (Optional[QueryParamTypes]): Query parameters to include in the request. Supports dict, list of tuples, or string.
            auth (Optional[AuthTypes]): Authentication to use for the request. Supports BasicAuth, DigestAuth, custom auth classes, or tuples for basic auth.
            timeout (int): Request timeout in seconds. Defaults to 10.

        Returns:
            Optional[httpx.Response]: The HTTP response if successful, None if failed

        Example:
            >>> # Using Basic Authentication
            >>> from httpx import BasicAuth
            >>> response = await client.execute_http_get_request(
            ...     url="https://api.example.com/data",
            ...     auth=BasicAuth("username", "password"),
            ...     params={"limit": 100}
            ... )
            >>>
            >>> # Using tuple for basic auth (username, password)
            >>> response = await client.execute_http_get_request(
            ...     url="https://api.example.com/data",
            ...     auth=("username", "password"),
            ...     params={"limit": 100}
            ... )
            >>>
            >>> # Using custom headers for Bearer token
            >>> response = await client.execute_http_get_request(
            ...     url="https://api.example.com/data",
            ...     headers={"Authorization": "Bearer token"},
            ...     params={"limit": 100}
            ... )
        """
        async with httpx.AsyncClient(
            timeout=timeout, transport=self.http_retry_transport
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
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error for {url}: {e.response.status_code}")
                return None
            except Exception as e:
                logger.error(f"Request failed for {url}: {e}")
                return None

    async def execute_http_post_request(
        self,
        url: str,
        data: Optional[RequestData] = None,
        json_data: Optional[Any] = None,
        content: Optional[bytes] = None,
        files: Optional[RequestFiles] = None,
        headers: Optional[HeaderTypes] = None,
        params: Optional[QueryParamTypes] = None,
        cookies: Optional[Dict[str, str]] = None,
        auth: Optional[AuthTypes] = None,
        follow_redirects: bool = True,
        verify: bool = True,
        timeout: int = 30,
    ) -> Optional[httpx.Response]:
        """
        Perform an HTTP POST request using the configured transport.

        This method uses httpx default transport which only retries on network-level errors
        (connection failures, timeouts). For status code-based retries (429, 500, etc.),
        consider overriding http_retry_transport in the load() method using httpx-retries library.

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
            verify (bool): Whether to verify SSL certificates. Defaults to True.
            timeout (int): Request timeout in seconds. Defaults to 30.

        Returns:
            Optional[httpx.Response]: The HTTP response if successful, None if failed

        Example:
            >>> # Basic JSON POST request with authentication
            >>> from httpx import BasicAuth
            >>> response = await client.execute_http_post_request(
            ...     url="https://api.example.com/data",
            ...     json_data={"name": "test", "value": 123},
            ...     headers={"Content-Type": "application/json"},
            ...     auth=BasicAuth("username", "password")
            ... )
            >>>
            >>> # File upload with basic auth tuple
            >>> with open("file.txt", "rb") as f:
            ...     response = await client.execute_http_post_request(
            ...         url="https://api.example.com/upload",
            ...         data={"description": "My file"},
            ...         files={"file": ("file.txt", f.read(), "text/plain")},
            ...         auth=("username", "password")
            ... )
        """
        async with httpx.AsyncClient(
            timeout=timeout, transport=self.http_retry_transport, verify=verify
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
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error for {url}: {e.response.status_code}")
                return None
            except Exception as e:
                logger.error(f"Request failed for {url}: {e}")
                return None
