import asyncio
import random
from typing import Any, Dict, Optional

import httpx

from application_sdk.clients import ClientInterface
from application_sdk.common.utils import send_heartbeat
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class GenericClient(ClientInterface):
    """
    Generic client for non-SQL based applications.

    This class provides a base implementation for clients that need to connect
    to non-SQL data sources. It implements the ClientInterface and provides
    basic functionality that can be extended by subclasses.

    Attributes:
        credentials (Dict[str, Any]): Client credentials for authentication.
    """

    def __init__(
        self,
        credentials: Dict[str, Any] = {},
    ):
        """
        Initialize the generic client.

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
        - Heartbeat support during long waits

        Args:
            url (str): The URL to make the GET request to
            headers (Optional[Dict[str, str]]): HTTP headers to include in the request
            params (Optional[Dict[str, Any]]): Query parameters to include in the request
            max_retries (int): Maximum number of retry attempts. Defaults to 3.
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
        while attempt < max_retries:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(url, headers=headers, params=params)

                    # Handle 401 Unauthorized - subclasses can create a custom function to handle auth errors
                    if response.status_code == 401:
                        logger.warning(
                            f"Received 401 Unauthorized (attempt {attempt + 1}/{max_retries}) (url={url})"
                        )
                        # Subclasses can override _handle_auth_error to implement token refresh
                        if hasattr(self, "_handle_auth_error"):
                            await self._handle_auth_error()
                            attempt += 1
                            continue
                        else:
                            logger.error(
                                "401 error - implement _handle_auth_error method to handle authentication"
                            )
                            return None

                    # Handle 429 Too Many Requests - wait and retry with exponential backoff
                    elif response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")

                        try:
                            if retry_after and retry_after.isdigit():
                                server_wait_time = int(retry_after)
                                wait_time = max(server_wait_time, base_wait_time)
                                logger.warning(
                                    f"Received 429 Too Many Requests (url={url}). Server suggests {server_wait_time}s, using {wait_time}s base wait time..."
                                )
                            else:
                                wait_time = base_wait_time
                                logger.warning(
                                    f"Received 429 with invalid Retry-After: '{retry_after}' (url={url}). Using default {wait_time}s base wait time..."
                                )
                        except Exception as e:
                            wait_time = base_wait_time
                            logger.error(
                                f"Received 429 Too Many Requests (url={url}). Error parsing Retry-After header: {e}. Using default {wait_time}s base wait time..."
                            )

                        # Apply exponential backoff with jitter
                        retry_number = attempt + 1
                        backoff_multiplier = 2 ** (retry_number - 1)
                        jitter = random.uniform(
                            0.8, 1.2
                        )  # Add jitter to prevent thundering herd
                        final_wait_time = int(wait_time * backoff_multiplier * jitter)

                        # Sleep with heartbeat and increment attempt
                        await self._sleep_with_heartbeat(final_wait_time)
                        attempt += 1
                        continue

                    # Handle expected failures - return None
                    elif response.status_code in [403, 404, 422, 423, 424, 425]:
                        status_messages = {
                            403: "Forbidden - User does not have permission",
                            404: "Not Found - Resource does not exist",
                            422: "Unprocessable Entity - Request validation failed",
                            423: "Locked - Resource is locked",
                            424: "Failed Dependency - Request failed due to dependency",
                            425: "Too Early - Request sent too early",
                        }
                        log_level = (
                            logger.debug
                            if response.status_code == 404
                            else logger.warning
                        )
                        log_level(
                            f"Received {response.status_code} {status_messages[response.status_code]} (url={url}): {response.text}"
                        )
                        return None

                    # Handle server errors - return None
                    elif response.status_code in [500, 503, 504]:
                        status_messages = {
                            500: "Internal Server Error",
                            503: "Service Unavailable",
                            504: "Gateway Timeout",
                        }
                        logger.error(
                            f"Received {response.status_code} {status_messages[response.status_code]} (url={url}): {response.text}"
                        )
                        return None

                    # Handle other errors
                    elif not response.is_success:
                        logger.error(
                            f"Request failed with status {response.status_code} (url={url}): {response.text}"
                        )
                        return None

                    # Success case
                    elif response.is_success:
                        return response

                    # send to retry - unaccounted for status code
                    else:
                        raise Exception(
                            f"Unexpected status code {response.status_code}: {response.text}"
                        )

            except httpx.RequestError as e:
                logger.warning(
                    f"Network error on attempt {attempt + 1}/{max_retries} (url={url}): {str(e)}"
                )

            except Exception as e:
                logger.warning(
                    f"Unexpected error on attempt {attempt + 1}/{max_retries} (url={url}): {str(e)}"
                )

            # Sleep with heartbeat and increment attempt
            await self._sleep_with_heartbeat(base_wait_time)
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
        - Heartbeat support during long waits

        Args:
            url (str): The URL to make the POST request to
            data (Optional[Dict[str, Any]]): Form data to send in the request body
            json_data (Optional[Dict[str, Any]]): JSON data to send in the request body
            headers (Optional[Dict[str, str]]): HTTP headers to include in the request
            params (Optional[Dict[str, Any]]): Query parameters to include in the request
            max_retries (int): Maximum number of retry attempts. Defaults to 3.
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
        while attempt < max_retries:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        url, data=data, json=json_data, headers=headers, params=params
                    )

                    # Handle 401 Unauthorized - subclasses can override to handle auth refresh
                    if response.status_code == 401:
                        logger.warning(
                            f"Received 401 Unauthorized (attempt {attempt + 1}/{max_retries}) (url={url})"
                        )
                        # Subclasses can override _handle_auth_error to implement token refresh
                        if hasattr(self, "_handle_auth_error"):
                            await self._handle_auth_error()
                            attempt += 1
                            continue
                        else:
                            logger.error(
                                "401 error - implement _handle_auth_error method to handle authentication"
                            )
                            return None

                    # Handle 429 Too Many Requests - wait and retry with exponential backoff
                    elif response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")

                        try:
                            if retry_after and retry_after.isdigit():
                                server_wait_time = int(retry_after)
                                wait_time = max(server_wait_time, base_wait_time)
                                logger.warning(
                                    f"Received 429 Too Many Requests (url={url}). Server suggests {server_wait_time}s, using {wait_time}s base wait time..."
                                )
                            else:
                                wait_time = base_wait_time
                                logger.warning(
                                    f"Received 429 with invalid Retry-After: '{retry_after}' (url={url}). Using default {wait_time}s base wait time..."
                                )
                        except Exception as e:
                            wait_time = base_wait_time
                            logger.error(
                                f"Received 429 Too Many Requests (url={url}). Error parsing Retry-After header: {e}. Using default {wait_time}s base wait time..."
                            )

                        # Apply exponential backoff with jitter
                        retry_number = attempt + 1
                        backoff_multiplier = 2 ** (retry_number - 1)
                        jitter = random.uniform(
                            0.8, 1.2
                        )  # Add jitter to prevent thundering herd
                        final_wait_time = int(wait_time * backoff_multiplier * jitter)

                        # Sleep with heartbeat and increment attempt
                        await self._sleep_with_heartbeat(final_wait_time)
                        attempt += 1
                        continue

                    # Handle expected failures - return None
                    elif response.status_code in [403, 404, 422, 423, 424, 425]:
                        status_messages = {
                            403: "Forbidden - User does not have permission",
                            404: "Not Found - Resource does not exist",
                            422: "Unprocessable Entity - Request validation failed",
                            423: "Locked - Resource is locked",
                            424: "Failed Dependency - Request failed due to dependency",
                            425: "Too Early - Request sent too early",
                        }
                        log_level = (
                            logger.debug
                            if response.status_code == 404
                            else logger.warning
                        )
                        log_level(
                            f"Received {response.status_code} {status_messages[response.status_code]} (url={url}): {response.text}"
                        )
                        return None

                    # Handle server errors - return None
                    elif response.status_code in [500, 503, 504]:
                        status_messages = {
                            500: "Internal Server Error",
                            503: "Service Unavailable",
                            504: "Gateway Timeout",
                        }
                        logger.error(
                            f"Received {response.status_code} {status_messages[response.status_code]} (url={url}): {response.text}"
                        )
                        return None

                    # Handle other errors
                    elif not response.is_success:
                        logger.error(
                            f"Request failed with status {response.status_code} (url={url}): {response.text}"
                        )
                        return None

                    # Success case
                    elif response.is_success:
                        return response

                    # send to retry - unaccounted for status code
                    else:
                        raise Exception(
                            f"Unexpected status code {response.status_code}: {response.text}"
                        )

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

            # Sleep with heartbeat and increment attempt
            await self._sleep_with_heartbeat(base_wait_time)
            attempt += 1
            continue

        logger.error(f"All {max_retries} attempts failed for URL: {url}")
        return None

    async def _sleep_with_heartbeat(self, sleep_time: int) -> None:
        """
        Sleep for the specified time while sending heartbeats every second.

        This method is useful for long-running operations that need to maintain
        activity heartbeats to prevent timeouts.

        Args:
            sleep_time (int): Total sleep time in seconds
        """
        for i in range(sleep_time):
            await asyncio.sleep(1)
            send_heartbeat()
