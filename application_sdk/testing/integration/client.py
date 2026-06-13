"""API client wrapper for integration testing.

This module provides a unified interface for calling the Core 3 APIs
(auth, preflight, workflow) during integration tests.

It wraps the existing APIServerClient and provides:
1. A mapping from API type strings to client methods
2. Better error handling (returns response instead of asserting)
3. Support for dynamic workflow endpoints
"""

import json
from typing import Any
from urllib.parse import urljoin

import requests

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def _to_v3_credentials(
    creds: dict[str, Any] | list[dict[str, str]],
) -> list[dict[str, str]]:
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

    pairs: list[dict[str, str]] = []
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


def _from_v3_credentials(
    creds: dict[str, Any] | list[dict[str, str]],
) -> dict[str, Any]:
    """Inverse of ``_to_v3_credentials`` — turn a v3 pair list into a flat dict.

    ``extra.<subkey>`` pairs are re-nested under a single ``extra`` dict. Dict
    inputs pass through unchanged so callers can stay format-agnostic.
    """
    if isinstance(creds, dict):
        return creds

    flat: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for pair in creds:
        key = pair.get("key")
        value = pair.get("value")
        if not key:
            continue
        if key.startswith("extra."):
            extra[key[len("extra.") :]] = value
        else:
            flat[key] = value
    if extra:
        flat["extra"] = extra
    return flat


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
        args: dict[str, Any],
        endpoint_override: str | None = None,
    ) -> dict[str, Any]:
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
            from application_sdk.testing.integration._errors import (  # noqa: PLC0415
                HttpClientInputError,
            )

            raise HttpClientInputError(
                message=(
                    f"Unsupported API type: '{api}'. "
                    f"Must be one of: auth, metadata, preflight, workflow, config"
                ),
                value_summary=api,
            )

    def _call_auth(self, args: dict[str, Any]) -> dict[str, Any]:
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

    def _call_metadata(self, args: dict[str, Any]) -> dict[str, Any]:
        """Call the metadata API.

        Sends v3-native credential format and optional connection_config.

        Args:
            args: Must contain "credentials" key. May contain
                "connection_config" and "object_filter".

        Returns:
            Dict[str, Any]: The API response.
        """
        creds = args.get("credentials", {})
        data: dict[str, Any] = {"credentials": _to_v3_credentials(creds)}
        if "connection_config" in args:
            data["connection_config"] = args["connection_config"]
        if "object_filter" in args:
            data["object_filter"] = args["object_filter"]
        return self._post("/metadata", data=data)

    def _call_preflight(self, args: dict[str, Any]) -> dict[str, Any]:
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
        data: dict[str, Any] = {
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
        args: dict[str, Any],
        endpoint_override: str | None = None,
    ) -> dict[str, Any]:
        """Call the workflow start API.

        Production /start strips inline credentials and resolves them via a
        ``credential_guid`` provisioned out-of-band (by AE/Heracles). To match
        that locally, we POST credentials to ``/dev/local-vault`` and forward
        only the returned guid. When local-vault is not exposed — e.g. SDR
        testcontainer runs where the workflow resolves credentials via
        ``agent_json`` — we fall back to sending inline credentials so those
        scenarios stay unaffected. Scenarios that already supply
        ``credential_guid`` skip provisioning entirely.

        Args:
            args: The workflow arguments (credentials, metadata, connection).
            endpoint_override: Optional override for the workflow endpoint.

        Returns:
            Dict[str, Any]: The API response.
        """
        endpoint = endpoint_override or self.workflow_endpoint
        data = dict(args)

        if "credential_guid" not in data and data.get("credentials"):
            credential_guid = self._provision_credentials(data["credentials"])
            if credential_guid is not None:
                data["credential_guid"] = credential_guid
                del data["credentials"]
            elif isinstance(data["credentials"], dict):
                data["credentials"] = _to_v3_credentials(data["credentials"])
        elif "credentials" in data and isinstance(data["credentials"], dict):
            data["credentials"] = _to_v3_credentials(data["credentials"])

        return self._post(endpoint, data=data)

    def _provision_credentials(
        self,
        credentials: dict[str, Any] | list[dict[str, str]],
    ) -> str | None:
        """Provision credentials via the dev local-vault and return the guid.

        Mirrors the production AE/Heracles flow: sensitive fields land in the
        local secret store, non-sensitive ones in object storage, and the
        caller gets back a guid to pass to ``/start``.

        When ``/dev/local-vault`` is gated off (HTTP 403 ``Dev-only endpoint``,
        e.g. on SDR testcontainer or any non-LOCAL deployment), returns
        ``None`` so the caller can fall back to inline credentials. Any other
        failure raises.

        Args:
            credentials: Flat ``{key: value}`` dict or v3 ``[{key, value}]``
                list. v3 lists are flattened back to a dict for the vault.

        Returns:
            The ``credential_guid`` issued by the vault, or ``None`` if the
            endpoint is gated off in this deployment.

        Raises:
            RuntimeError: If local-vault cannot be reached, or is reachable but
                returns no guid.
        """
        flat = _from_v3_credentials(credentials)
        response = self._post("/dev/local-vault", data=flat)

        if response.get("_http_status") == 403:
            logger.info(
                "local-vault gated off (%s); falling back to inline credentials. "
                "If your workflow expects a credential_guid, set one explicitly "
                "on the scenario.",
                response.get("detail") or response.get("error"),
            )
            return None

        # Transport-failure shape from _post (ConnectionError / Timeout /
        # RequestException): no _http_status, success=False, error is a dict.
        # Surface the actual cause instead of the generic missing-guid raise.
        if "_http_status" not in response and isinstance(response.get("error"), dict):
            err = response["error"]
            raise RuntimeError(
                f"Could not reach /dev/local-vault at {self.host}: "
                f"{err.get('message')}. Is the application server running?"
            )

        guid = (response.get("data") or {}).get("credential_guid") or response.get(
            "credential_guid"
        )
        if not guid:
            raise RuntimeError(
                "Local-vault did not return a credential_guid "
                f"(status={response.get('_http_status')})."
            )
        logger.debug("Provisioned credentials via local-vault: guid=%s", guid)
        return guid

    def _call_config(self, args: dict[str, Any]) -> dict[str, Any]:
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

    def get_config(self, workflow_id: str) -> dict[str, Any]:
        """Get the configuration for a workflow.

        Args:
            workflow_id: The workflow ID.

        Returns:
            Dict[str, Any]: The config response.
        """
        return self._get(f"/config/{workflow_id}")

    def update_config(
        self, workflow_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        """Get the status of a workflow execution.

        Args:
            workflow_id: The workflow ID.
            run_id: The run ID.

        Returns:
            Dict[str, Any]: The workflow status response.
        """
        return self._get(f"/status/{workflow_id}/{run_id}")

    def _get(self, endpoint: str) -> dict[str, Any]:
        """Make a GET request to the API.

        Args:
            endpoint: The API endpoint (relative to base_url).

        Returns:
            Dict[str, Any]: The response as a dictionary.

        Raises:
            requests.RequestException: If the request fails.
        """
        url = f"{self.base_url}{endpoint}"
        logger.debug("GET %s", url)

        try:
            response = requests.get(url, timeout=self.timeout)
            return self._handle_response(response)
        except requests.ConnectionError as e:
            logger.error(
                "GET request failed - cannot connect to %s", url, exc_info=True
            )
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
            logger.error("GET request timed out after %ss", self.timeout, exc_info=True)
            return {
                "success": False,
                "error": {
                    "code": "REQUEST_TIMEOUT",
                    "message": f"Request to {url} timed out after {self.timeout}s",
                    "details": str(e),
                },
            }
        except requests.RequestException as e:
            logger.error("GET request failed", exc_info=True)
            return {
                "success": False,
                "error": {
                    "code": "REQUEST_FAILED",
                    "message": str(e),
                },
            }

    def _post(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
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
        logger.debug("POST %s", url)

        try:
            response = requests.post(url, json=data, timeout=self.timeout)
            return self._handle_response(response)
        except requests.ConnectionError as e:
            logger.error(
                "POST request failed - cannot connect to %s", url, exc_info=True
            )
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
            logger.error(
                "POST request timed out after %ss", self.timeout, exc_info=True
            )
            return {
                "success": False,
                "error": {
                    "code": "REQUEST_TIMEOUT",
                    "message": f"Request to {url} timed out after {self.timeout}s",
                    "details": str(e),
                },
            }
        except requests.RequestException as e:
            logger.error("POST request failed", exc_info=True)
            return {
                "success": False,
                "error": {
                    "code": "REQUEST_FAILED",
                    "message": str(e),
                },
            }

    def _handle_response(self, response: requests.Response) -> dict[str, Any]:
        """Handle the HTTP response and convert to dictionary.

        Args:
            response: The requests Response object.

        Returns:
            Dict[str, Any]: The response as a dictionary with status info.
        """
        try:
            result = response.json()
        except ValueError:  # conformance: ignore[E009] non-JSON response; synthetic error result dict is the explicit fallback
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
