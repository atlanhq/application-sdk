"""API client wrapper for integration testing.

This module provides a unified interface for calling the Core 3 APIs
(auth, preflight, workflow) during integration tests.

It wraps the existing APIServerClient and provides:
1. A mapping from API type strings to client methods
2. Better error handling (returns response instead of asserting)
3. Support for dynamic workflow endpoints
"""

from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin

import requests

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class IntegrationTestClient:
    """Client for integration testing of the Core 3 APIs.

    This client wraps HTTP calls to the application server and provides
    a unified interface for auth, preflight, and workflow operations.

    Unlike the E2E client which asserts on status codes, this client
    returns the full response for the test framework to validate.

    Attributes:
        host: The base URL of the application server.
        version: API version prefix (default: "v1").
        workflow_endpoint: The endpoint for starting workflows (default: "/start").
        timeout: Request timeout in seconds.

    Example:
        >>> client = IntegrationTestClient(host="http://localhost:8000")
        >>> response = client.call_api("auth", {"credentials": {...}})
        >>> print(response["success"])
    """

    def __init__(
        self,
        host: str,
        version: str = "v1",
        workflow_endpoint: str = "/start",
        timeout: int = 30,
    ):
        """Initialize the integration test client.

        Args:
            host: The base URL of the application server.
            version: API version prefix.
            workflow_endpoint: The endpoint for starting workflows.
            timeout: Request timeout in seconds.
        """
        self.host = host
        self.version = version
        self.workflow_endpoint = workflow_endpoint
        self.timeout = timeout
        self.base_url = urljoin(host, f"workflows/{version}")

    def call_api(
        self,
        api: str,
        args: Dict[str, Any],
        endpoint_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Call an API based on the API type.

        This is the main entry point for the test framework. It routes
        the call to the appropriate API method based on the api parameter.

        Args:
            api: The API type ("auth", "preflight", "workflow").
            args: The arguments to pass to the API.
            endpoint_override: Optional override for the workflow endpoint.

        Returns:
            Dict[str, Any]: The API response as a dictionary.

        Raises:
            ValueError: If the API type is not supported.
            requests.RequestException: If the HTTP request fails.
        """
        api_lower = api.lower()

        if api_lower == "auth":
            return self._call_auth(args)
        elif api_lower == "preflight":
            return self._call_preflight(args)
        elif api_lower == "workflow":
            return self._call_workflow(args, endpoint_override)
        else:
            raise ValueError(
                f"Unsupported API type: '{api}'. "
                f"Must be one of: auth, preflight, workflow"
            )

    def _call_auth(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call the authentication API.

        Args:
            args: Must contain "credentials" key.

        Returns:
            Dict[str, Any]: The API response.
        """
        credentials = args.get("credentials", args)
        return self._post("/auth", data=credentials)

    def _call_preflight(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call the preflight check API.

        Args:
            args: Must contain "credentials" and "metadata" keys.

        Returns:
            Dict[str, Any]: The API response.
        """
        data = {
            "credentials": args.get("credentials", {}),
            "metadata": args.get("metadata", {}),
        }
        return self._post("/check", data=data)

    def _call_workflow(
        self,
        args: Dict[str, Any],
        endpoint_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Call the workflow start API.

        Args:
            args: The workflow arguments (credentials, metadata, connection).
            endpoint_override: Optional override for the workflow endpoint.

        Returns:
            Dict[str, Any]: The API response.
        """
        endpoint = endpoint_override or self.workflow_endpoint
        return self._post(endpoint, data=args)

    def get_workflow_status(
        self,
        workflow_id: str,
        run_id: str,
    ) -> Dict[str, Any]:
        """Get the status of a workflow execution.

        Args:
            workflow_id: The workflow ID.
            run_id: The run ID.

        Returns:
            Dict[str, Any]: The workflow status response.
        """
        return self._get(f"/status/{workflow_id}/{run_id}")

    def _get(self, endpoint: str) -> Dict[str, Any]:
        """Make a GET request to the API.

        Args:
            endpoint: The API endpoint (relative to base_url).

        Returns:
            Dict[str, Any]: The response as a dictionary.

        Raises:
            requests.RequestException: If the request fails.
        """
        url = f"{self.base_url}{endpoint}"
        logger.debug(f"GET {url}")

        try:
            response = requests.get(url, timeout=self.timeout)
            return self._handle_response(response)
        except requests.RequestException as e:
            logger.error(f"GET request failed: {e}")
            return {
                "success": False,
                "error": {
                    "code": "REQUEST_FAILED",
                    "message": str(e),
                },
            }

    def _post(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Make a POST request to the API.

        Args:
            endpoint: The API endpoint (relative to base_url).
            data: The request body.

        Returns:
            Dict[str, Any]: The response as a dictionary.

        Raises:
            requests.RequestException: If the request fails.
        """
        url = f"{self.base_url}{endpoint}"
        logger.debug(f"POST {url}")

        try:
            response = requests.post(url, json=data, timeout=self.timeout)
            return self._handle_response(response)
        except requests.RequestException as e:
            logger.error(f"POST request failed: {e}")
            return {
                "success": False,
                "error": {
                    "code": "REQUEST_FAILED",
                    "message": str(e),
                },
            }

    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        """Handle the HTTP response and convert to dictionary.

        Args:
            response: The requests Response object.

        Returns:
            Dict[str, Any]: The response as a dictionary with status info.
        """
        try:
            result = response.json()
        except ValueError:
            # Response is not JSON
            result = {
                "success": False,
                "error": {
                    "code": "INVALID_RESPONSE",
                    "message": "Response is not valid JSON",
                    "body": response.text[:500] if response.text else None,
                },
            }

        # Add HTTP status info if not present
        if "_http_status" not in result:
            result["_http_status"] = response.status_code

        return result


# =============================================================================
# API Method Mapping (Higher-Order Function Pattern)
# =============================================================================

# Type alias for API method functions
APIMethod = Callable[[IntegrationTestClient, Dict[str, Any]], Dict[str, Any]]


def create_api_method_map() -> Dict[str, APIMethod]:
    """Create a mapping of API types to client methods.

    This uses higher-order functions to create a flexible mapping
    that can be extended or customized.

    Returns:
        Dict[str, APIMethod]: Mapping of API type strings to callable methods.
    """
    return {
        "auth": lambda client, args: client._call_auth(args),
        "preflight": lambda client, args: client._call_preflight(args),
        "workflow": lambda client, args: client._call_workflow(args),
    }


# Default API method map
API_METHODS = create_api_method_map()
