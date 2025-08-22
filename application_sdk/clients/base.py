import asyncio
import random
from typing import Any, Dict, Optional, Tuple

import httpx

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

    Extending the Client:
        To handle authentication errors (401 responses), subclasses must implement
        the `_handle_auth_error()` method. This method is called automatically
        when a 401 Unauthorized response is received, allowing for token refresh
        or other authentication recovery mechanisms.

        Example:
            >>> class MyClient(BaseClient):
            ...     async def _handle_auth_error(self):
            ...         # Implement token refresh logic here
            ...         await self.refresh_token()
            ...         # Update credentials or headers as needed
    """

    def __init__(
        self,
        credentials: Dict[str, Any] = {},
    ):
        """
        Initialize the base client.

        Args:
            credentials (Dict[str, Any], optional): Client credentials for authentication. Defaults to {}.
        """
        self.credentials = credentials

    async def load(self, **kwargs: Any) -> None:
        """
        Initialize the client with credentials and necessary attributes for the client to work.

        Args:
            **kwargs: Additional keyword arguments, typically including credentials.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("load method is not implemented")

    async def _handle_auth_error(self) -> None:
        """
        Handle authentication errors (401 Unauthorized responses).

        This method is called automatically when a 401 Unauthorized response is received
        during HTTP requests. Subclasses should override this method to implement
        authentication recovery mechanisms such as token refresh.

        The method is called before retrying the request, allowing the subclass to:
        - Refresh expired tokens
        - Update authentication headers
        - Re-authenticate with the service
        - Update stored credentials

        Example:
            >>> async def _handle_auth_error(self):
            ...     # Refresh the access token
            ...     new_token = await self.refresh_access_token()
            ...     # Update the authorization header
            ...     self.auth_headers = {"Authorization": f"Bearer {new_token}"}

        Note:
            This method is optional. If not implemented, 401 errors will not trigger
            retries and will result in request failure.
        """
        # This is a hook method - subclasses should override to implement auth recovery
        raise NotImplementedError(
            "Subclasses must implement _handle_auth_error to handle authentication errors"
        )

    async def _handle_http_response(
        self,
        response: httpx.Response,
        url: str,
        attempt: int,
        max_retries: int,
        base_wait_time: int,
    ) -> Tuple[Optional[httpx.Response], bool]:
        """
        Handle HTTP response and determine if retry is needed.

        Args:
            response: The HTTP response to handle
            url: The URL that was requested
            attempt: Current attempt number
            max_retries: Maximum number of retry attempts
            base_wait_time: Base wait time for exponential backoff

        Returns:
            Tuple of (response_or_none, should_retry)
        """
        # Get status phrase once for all cases
        try:
            status_phrase = httpx.codes.get_reason_phrase(response.status_code)
        except ValueError:
            # Handle non-standard status codes
            status_phrase = f"Unknown Status {response.status_code}"

        # Success case - return immediately
        if response.is_success:
            return response, False  # Don't retry, return success

        # Handle 401 Unauthorized - subclasses can create a custom function to handle auth errors
        elif response.status_code == httpx.codes.UNAUTHORIZED:
            logger.warning(
                f"Received 401 {status_phrase} (attempt {attempt + 1}/{max_retries}) (url={url})"
            )
            # Subclasses can override _handle_auth_error to implement token refresh
            try:
                await self._handle_auth_error()
                logger.info("Auth error handler called successfully, retrying request")
                return None, True  # Retry after auth refresh
            except NotImplementedError:
                logger.error(
                    "401 error - implement _handle_auth_error method to handle authentication"
                )
                return None, False  # Don't retry
            except Exception as e:
                logger.error(f"Auth error handler failed: {e}")
                return None, False  # Don't retry on auth handler failure

        # Handle 429 Too Many Requests - wait and retry with exponential backoff
        elif response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            retry_after = response.headers.get("Retry-After")

            try:
                if retry_after and retry_after.isdigit():
                    server_wait_time = int(retry_after)
                    wait_time = max(server_wait_time, base_wait_time)
                    logger.warning(
                        f"Received 429 {status_phrase} (url={url}). Server suggests {server_wait_time}s, using {wait_time}s base wait time..."
                    )
                else:
                    wait_time = base_wait_time
                    logger.warning(
                        f"Received 429 with invalid Retry-After: '{retry_after}' (url={url}). Using default {wait_time}s base wait time..."
                    )
            except Exception as e:
                wait_time = base_wait_time
                logger.warning(
                    f"Received 429 {status_phrase} (url={url}). Error parsing Retry-After header: {e}. Using default {wait_time}s base wait time..."
                )

            # Apply exponential backoff with jitter
            retry_number = attempt + 1
            backoff_multiplier = 2 ** (retry_number - 1)
            jitter = random.uniform(0.8, 1.2)  # Add jitter to prevent thundering herd
            final_wait_time = int(wait_time * backoff_multiplier * jitter)

            # Sleep and increment attempt
            await asyncio.sleep(final_wait_time)
            return None, True  # Retry after waiting

        # Handle all other error status codes
        else:
            logger.error(
                f"Request failed with status {response.status_code} {status_phrase} (url={url}): {response.text}"
            )
            return None, False  # Don't retry

    async def execute_http_get_request(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        base_wait_time: int = 10,
        timeout: int = 10,
    ) -> Optional[httpx.Response]:
        """
        Perform an HTTP GET request with comprehensive retry logic and error handling.

        This method provides a HTTP GET request implementation with:
        - Automatic retry logic with exponential backoff for network errors and unexpected errors
        - Rate limiting support (429 responses) with exponential backoff
        - Extendable authentication error handling (401 responses) support with subclass override for function _handle_auth_error

        Args:
            url (str): The URL to make the GET request to
            headers (Optional[Dict[str, str]]): HTTP headers to include in the request
            params (Optional[Dict[str, Any]]): Query parameters to include in the request
            max_retries (int): Maximum number of retry attempts. Defaults to 3. Max is 10.
            base_wait_time (int): Base wait time in seconds for exponential backoff. Defaults to 10.
            timeout (int): Request timeout in seconds. Defaults to 10.

        Returns:
            Optional[httpx.Response]: The HTTP response if successful, None if failed after all retries

        Example:
            >>> response = await client.execute_http_get_request(
            ...     url="https://api.example.com/data",
            ...     headers={"Authorization": "Bearer token"},
            ...     params={"limit": 100}
            ... )
        """
        attempt = 0
        while attempt < min(max_retries, 10):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(url, headers=headers, params=params)

                    result, should_retry = await self._handle_http_response(
                        response, url, attempt, max_retries, base_wait_time
                    )

                    if should_retry:
                        attempt += 1
                        continue
                    else:
                        return result

            except httpx.RequestError as e:
                logger.warning(
                    f"Network error on attempt {attempt + 1}/{max_retries} (url={url}): {str(e)}"
                )

            except Exception as e:
                logger.warning(
                    f"Unexpected error on attempt {attempt + 1}/{max_retries} (url={url}): {str(e)}"
                )

            # Sleep and increment attempt
            await asyncio.sleep(base_wait_time)
            attempt += 1
            continue

        logger.error(f"All {max_retries} attempts failed for URL: {url}")
        return None

    async def execute_http_post_request(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        base_wait_time: int = 10,
        timeout: int = 30,
    ) -> Optional[httpx.Response]:
        """
        Perform an HTTP POST request with comprehensive retry logic and error handling.

        This method provides a HTTP POST request implementation with:
        - Automatic retry logic with exponential backoff for network errors and unexpected errors
        - Rate limiting support (429 responses) with exponential backoff
        - Extendable authentication error handling (401 responses) support with subclass override for function _handle_auth_error

        Args:
            url (str): The URL to make the POST request to
            data (Optional[Dict[str, Any]]): Form data to send in the request body
            json_data (Optional[Dict[str, Any]]): JSON data to send in the request body
            headers (Optional[Dict[str, str]]): HTTP headers to include in the request
            params (Optional[Dict[str, Any]]): Query parameters to include in the request
            max_retries (int): Maximum number of retry attempts. Defaults to 3. Max is 10.
            base_wait_time (int): Base wait time in seconds for exponential backoff. Defaults to 10.
            timeout (int): Request timeout in seconds. Defaults to 30.

        Returns:
            Optional[httpx.Response]: The HTTP response if successful, None if failed after all retries

        Example:
            >>> response = await client.execute_http_post_request(
            ...     url="https://api.example.com/data",
            ...     json_data={"name": "test", "value": 123},
            ...     headers={"Authorization": "Bearer token"}
            ... )
        """
        attempt = 0
        while attempt < min(max_retries, 10):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        url, data=data, json=json_data, headers=headers, params=params
                    )

                    result, should_retry = await self._handle_http_response(
                        response, url, attempt, max_retries, base_wait_time
                    )

                    if should_retry:
                        attempt += 1
                        continue
                    else:
                        return result

            except httpx.RequestError as e:
                log_level = (
                    logger.warning if attempt < max_retries - 1 else logger.error
                )
                log_level(
                    f"Network error on attempt {attempt + 1}/{max_retries} (url={url}): {str(e)}"
                )

            except Exception as e:
                log_level = (
                    logger.warning if attempt < max_retries - 1 else logger.error
                )
                log_level(
                    f"Unexpected error on attempt {attempt + 1}/{max_retries} (url={url}): {str(e)}"
                )

            # Sleep and increment attempt
            await asyncio.sleep(base_wait_time)
            attempt += 1
            continue

        logger.error(f"All {max_retries} attempts failed for URL: {url}")
        return None
