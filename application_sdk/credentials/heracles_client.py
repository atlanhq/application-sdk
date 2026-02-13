"""Heracles API client for credential resolution.

This client communicates with the Heracles backend to resolve
credential mappings from JWT tokens.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from application_sdk.credentials.exceptions import CredentialResolutionError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Environment variable for Heracles URL
HERACLES_URL_ENV = "HERACLES_URL"
DEFAULT_HERACLES_URL = "http://heracles:8080"


@dataclass
class SlotMapping:
    """Mapping for a single credential slot.

    Attributes:
        slot_name: Name of the credential slot (as declared in handler).
        credential_guid: GUID of the credential in the secret store.
        field_mappings: Optional mapping from secret store field names
            to credential declaration field names.
    """

    slot_name: str
    credential_guid: str
    field_mappings: Dict[str, str] = field(default_factory=dict)


@dataclass
class CredentialMappingResponse:
    """Response from Heracles credential mapping API.

    Attributes:
        slot_mappings: List of slot mappings with optional field mappings.
    """

    slot_mappings: List[SlotMapping]


class HeraclesClient:
    """Client for Heracles credential resolution API.

    This client resolves credential mappings from JWT tokens. The JWT
    is extracted from Temporal headers and exchanged for credential
    slot mappings including field name translations.

    The flow is:
    1. Worker receives x-credential-jwt from Temporal headers
    2. Worker calls Heracles /oauth-clients/credential-mapping with JWT
    3. Heracles validates JWT and returns:
       - slot -> credential_guid mapping
       - field mappings (secret store field -> app field)
    4. Worker fetches secrets from Dapr secret store using GUIDs
    5. Worker applies field mappings to translate field names

    Dapr abstracts the actual secret backend (Vault, AWS Secrets Manager,
    customer environment, external secret managers, etc.)

    Example:
        >>> client = HeraclesClient()
        >>> response = await client.resolve_credential_mapping(jwt_token)
        >>> for slot in response.slot_mappings:
        ...     print(f"{slot.slot_name} -> {slot.credential_guid}")
        ...     print(f"  field mappings: {slot.field_mappings}")
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        """Initialize the Heracles client.

        Args:
            base_url: Heracles API URL. Defaults to HERACLES_URL env or localhost.
            timeout: Request timeout in seconds.
        """
        self._base_url = base_url or os.environ.get(
            HERACLES_URL_ENV, DEFAULT_HERACLES_URL
        )
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def resolve_credential_mapping(
        self,
        jwt_token: str,
    ) -> CredentialMappingResponse:
        """Resolve credential mapping from JWT token.

        Calls Heracles to validate the JWT and retrieve the
        slot mappings including field name translations.

        Args:
            jwt_token: The x-credential-jwt from Temporal headers.

        Returns:
            CredentialMappingResponse with slot mappings and field mappings.

        Raises:
            CredentialResolutionError: If resolution fails.

        Example:
            >>> response = await client.resolve_credential_mapping(jwt)
            >>> for slot in response.slot_mappings:
            ...     print(f"{slot.slot_name}: {slot.credential_guid}")
            ...     print(f"  fields: {slot.field_mappings}")
        """
        client = await self._get_client()

        try:
            response = await client.get(
                "/oauth-clients/credential-mapping",
                headers={"Authorization": f"Bearer {jwt_token}"},
            )

            if response.status_code == 401:
                raise CredentialResolutionError(
                    "JWT token is invalid or expired. "
                    "The workflow may need to be restarted with fresh credentials."
                )

            if response.status_code == 403:
                raise CredentialResolutionError(
                    "Access denied to credential mapping. Check profile permissions."
                )

            if response.status_code != 200:
                raise CredentialResolutionError(
                    f"Failed to resolve credential mapping: "
                    f"HTTP {response.status_code} - {response.text}"
                )

            data = response.json()

            # Expected response format:
            # {
            #   "slotMappings": [
            #     {
            #       "slotName": "database",
            #       "credentialGuid": "cred-123",
            #       "fieldMappings": {
            #         "db_password": "password",
            #         "db_user": "username"
            #       }
            #     },
            #     {
            #       "slotName": "api",
            #       "credentialGuid": "cred-456",
            #       "fieldMappings": {}
            #     }
            #   ]
            # }
            raw_mappings = data.get("slotMappings", [])
            slot_mappings = [
                SlotMapping(
                    slot_name=item["slotName"],
                    credential_guid=item["credentialGuid"],
                    field_mappings=item.get("fieldMappings", {}),
                )
                for item in raw_mappings
            ]

            logger.debug(
                f"Resolved credential mapping with {len(slot_mappings)} slots",
                extra={"slots": [s.slot_name for s in slot_mappings]},
            )

            return CredentialMappingResponse(slot_mappings=slot_mappings)

        except httpx.RequestError as e:
            raise CredentialResolutionError(
                f"Failed to connect to Heracles: {e}"
            ) from e

    async def validate_jwt(self, jwt_token: str) -> bool:
        """Validate a JWT token without resolving credentials.

        Args:
            jwt_token: The JWT token to validate.

        Returns:
            True if token is valid.

        Raises:
            CredentialResolutionError: If validation fails.
        """
        client = await self._get_client()

        try:
            response = await client.get(
                "/oauth-clients/validate",
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
            return response.status_code == 200
        except httpx.RequestError as e:
            raise CredentialResolutionError(f"Failed to validate JWT: {e}") from e

    async def refresh_jwt(
        self,
        refresh_token: str,
    ) -> Dict[str, str]:
        """Refresh JWT token using refresh token.

        For long-running workflows, the initial JWT may expire.
        This method uses the refresh token to obtain a new JWT.

        Args:
            refresh_token: The x-credential-refresh-token from Temporal headers.

        Returns:
            Dictionary with new access_token and optional refresh_token.

        Raises:
            CredentialResolutionError: If refresh fails.
        """
        client = await self._get_client()

        try:
            response = await client.post(
                "/oauth-clients/refresh",
                json={"refresh_token": refresh_token},
            )

            if response.status_code != 200:
                raise CredentialResolutionError(
                    f"Failed to refresh JWT: HTTP {response.status_code}"
                )

            data = response.json()
            return {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", refresh_token),
            }

        except httpx.RequestError as e:
            raise CredentialResolutionError(f"Failed to refresh JWT: {e}") from e

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "HeraclesClient":
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


def apply_field_mappings(
    credentials: Dict[str, Any],
    field_mappings: Dict[str, str],
) -> Dict[str, Any]:
    """Apply field mappings to transform credential field names.

    Transforms credentials from secret store field names to the field names
    expected by the credential declaration.

    Args:
        credentials: Raw credentials from secret store.
        field_mappings: Mapping from secret store field name to app field name.
            e.g., {"db_password": "password", "db_user": "username"}

    Returns:
        Credentials with field names transformed according to mappings.
        Fields not in the mapping are passed through unchanged.

    Example:
        >>> creds = {"db_password": "secret123", "db_user": "admin", "host": "localhost"}
        >>> mappings = {"db_password": "password", "db_user": "username"}
        >>> result = apply_field_mappings(creds, mappings)
        >>> # result: {"password": "secret123", "username": "admin", "host": "localhost"}
    """
    if not field_mappings:
        return credentials

    result: Dict[str, Any] = {}

    for store_field, value in credentials.items():
        # Check if this field should be renamed
        if store_field in field_mappings:
            app_field = field_mappings[store_field]
            result[app_field] = value
        else:
            # Pass through unchanged
            result[store_field] = value

    return result
