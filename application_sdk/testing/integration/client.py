"""API client wrapper for integration testing.

This module provides a unified interface for calling the Core 3 APIs
(auth, preflight, workflow) during integration tests.

It wraps the existing APIServerClient and provides:
1. A mapping from API type strings to client methods
2. Better error handling (returns response instead of asserting)
3. Support for dynamic workflow endpoints
"""

import json
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import urljoin

import requests

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def _to_v3_credentials(
    creds: Union[Dict[str, Any], List[Dict[str, str]]],
) -> List[Dict[str, str]]:
    """Convert a flat credential dict to v3 ``[{"key": k, "value": v}]`` format.

    If *creds* is already a list (v3 format), it is returned as-is.
    Dict values are stringified (``HandlerCredential.value`` is ``str``).
    The ``extra`` key receives special handling: nested keys are flattened
    to ``extra.<subkey>`` to match the server-side ``_flatten_to_pairs``.

    Args:
        creds: Either a flat dict (v2) or a list of key-value pairs (v3).

    Returns:
        List of ``{"key": ..., "value": ...}`` dicts.
    """
    if isinstance(creds, list):
        return creds

    pairs: List[Dict[str, str]] = []
    # Shallow-copy to avoid mutating the caller's dict
    creds = dict(creds)
    extra = creds.pop("extra", None)

    for k, v in creds.items():
        if v is not None:
            str_v = (
                v
                if isinstance(v, str)
                else json.dumps(v)
                if isinstance(v, (dict, list, bool))
                else str(v)
            )
            pairs.append({"key": k, "value": str_v})

    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is not None:
                str_v = (
                    v
                    if isinstance(v, str)
                    else json.dumps(v)
                    if isinstance(v, (dict, list, bool))
                    else str(v)
                )
                pairs.append({"key": f"extra.{k}", "value": str_v})

    return pairs


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
        elif api_lower == "metadata":
            return self._call_metadata(args)
        elif api_lower == "preflight":
            return self._call_preflight(args)
        elif api_lower == "workflow":
            return self._call_workflow(args, endpoint_override)
        elif api_lower == "config":
            return self._call_config(args)
        else:
            raise ValueError(
                f"Unsupported API type: '{api}'. "
                f"Must be one of: auth, metadata, preflight, workflow, config"
            )

    def _call_auth(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call the authentication API.

        Sends v3-native credential format (list of key-value pairs).
        Flat credential dicts are auto-converted.

        Args:
            args: Must contain "credentials" key (dict or list).

        Returns:
            Dict[str, Any]: The API response.
        """
        creds = args.get("credentials", args)
        data = {"credentials": _to_v3_credentials(creds)}
        return self._post("/auth", data=data)

    def _call_metadata(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call the metadata API.

        Sends v3-native credential format and optional connection_config.

        Args:
            args: Must contain "credentials" key. May contain
                "connection_config" and "object_filter".

        Returns:
            Dict[str, Any]: The API response.
        """
        creds = args.get("credentials", {})
        data: Dict[str, Any] = {"credentials": _to_v3_credentials(creds)}
        if "connection_config" in args:
            data["connection_config"] = args["connection_config"]
        if "object_filter" in args:
            data["object_filter"] = args["object_filter"]
        return self._post("/metadata", data=data)

    def _call_preflight(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call the preflight check API.

        Sends v3-native credential format and connection_config.
        Falls back to ``metadata`` when ``connection_config`` is absent
        for backward compatibility with v2-style scenarios.

        Args:
            args: Must contain "credentials". May contain "connection_config"
                (v3) or "metadata" (v2 compat), "checks_to_run", and
                "timeout_seconds".

        Returns:
            Dict[str, Any]: The API response.
        """
        creds = args.get("credentials", {})
        data: Dict[str, Any] = {
            "credentials": _to_v3_credentials(creds),
            "connection_config": args.get(
                "connection_config", args.get("metadata", {})
            ),
        }
        if "checks_to_run" in args:
            data["checks_to_run"] = args["checks_to_run"]
        if "timeout_seconds" in args:
            data["timeout_seconds"] = args["timeout_seconds"]
        return self._post("/check", data=data)

    def _call_workflow(
        self,
        args: Dict[str, Any],
        endpoint_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Call the workflow start API.

        Converts flat credential dicts to v3 key-value format before sending.

        Args:
            args: The workflow arguments (credentials, metadata, connection).
            endpoint_override: Optional override for the workflow endpoint.

        Returns:
            Dict[str, Any]: The API response.
        """
        endpoint = endpoint_override or self.workflow_endpoint
        # Convert credentials inside args to v3 format if present
        data = dict(args)
        if "credentials" in data and isinstance(data["credentials"], dict):
            data["credentials"] = _to_v3_credentials(data["credentials"])
        return self._post(endpoint, data=data)

    def _call_config(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call the config GET or POST API.

        Args:
            args: Must contain "config_action" ("get" or "update"),
                  "config_workflow_id", and optionally "config_payload".

        Returns:
            Dict[str, Any]: The API response.
        """
        action = args.get("config_action", "get")
        workflow_id = args.get("config_workflow_id")

        if not workflow_id:
            return {
                "success": False,
                "error": "config_workflow_id is required for config API calls",
            }

        if action == "get":
            return self.get_config(workflow_id)
        elif action == "update":
            payload = args.get("config_payload", {})
            return self.update_config(workflow_id, payload)
        else:
            return {
                "success": False,
                "error": f"Invalid config_action: '{action}'. Must be 'get' or 'update'",
            }

    def get_config(self, workflow_id: str) -> Dict[str, Any]:
        """Get the configuration for a workflow.

        Args:
            workflow_id: The workflow ID.

        Returns:
            Dict[str, Any]: The config response.
        """
        return self._get(f"/config/{workflow_id}")

    def update_config(
        self, workflow_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update the configuration for a workflow.

        Args:
            workflow_id: The workflow ID.
            payload: The config update payload (connection, metadata).

        Returns:
            Dict[str, Any]: The config response.
        """
        return self._post(f"/config/{workflow_id}", data=payload)

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
        logger.debug(f"GET {url}")  # noqa: G004

        try:
            response = requests.get(url, timeout=self.timeout)
            return self._handle_response(response)
        except requests.ConnectionError as e:
            logger.error(f"GET request failed - cannot connect to {url}: {e}")  # noqa: G004
            return {
                "success": False,
                "error": {
                    "code": "CONNECTION_FAILED",
                    "message": (
                        f"Cannot connect to server at {self.host}. "
                        f"Is the application running? Start it with: uv run python main.py"
                    ),
                    "details": str(e),
                },
            }
        except requests.Timeout as e:
            logger.error(f"GET request timed out after {self.timeout}s: {e}")  # noqa: G004
            return {
                "success": False,
                "error": {
                    "code": "REQUEST_TIMEOUT",
                    "message": f"Request to {url} timed out after {self.timeout}s",
                    "details": str(e),
                },
            }
        except requests.RequestException as e:
            logger.error(f"GET request failed: {e}")  # noqa: G004
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
        logger.debug(f"POST {url}")  # noqa: G004

        try:
            response = requests.post(url, json=data, timeout=self.timeout)
            return self._handle_response(response)
        except requests.ConnectionError as e:
            logger.error(f"POST request failed - cannot connect to {url}: {e}")  # noqa: G004
            return {
                "success": False,
                "error": {
                    "code": "CONNECTION_FAILED",
                    "message": (
                        f"Cannot connect to server at {self.host}. "
                        f"Is the application running? Start it with: uv run python main.py"
                    ),
                    "details": str(e),
                },
            }
        except requests.Timeout as e:
            logger.error(f"POST request timed out after {self.timeout}s: {e}")  # noqa: G004
            return {
                "success": False,
                "error": {
                    "code": "REQUEST_TIMEOUT",
                    "message": f"Request to {url} timed out after {self.timeout}s",
                    "details": str(e),
                },
            }
        except requests.RequestException as e:
            logger.error(f"POST request failed: {e}")  # noqa: G004
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
        "metadata": lambda client, args: client._call_metadata(args),
        "preflight": lambda client, args: client._call_preflight(args),
        "workflow": lambda client, args: client._call_workflow(args),
        "config": lambda client, args: client._call_config(args),
    }


# Default API method map
API_METHODS = create_api_method_map()
