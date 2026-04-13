"""API-key auth strategy."""

from __future__ import annotations

from typing import Any

from application_sdk.credentials.types import ApiKeyCredential, Credential


class ApiKeyAuthStrategy:
    """Handles ApiKeyCredential — primarily for REST API clients.

    Returns the API key as an HTTP header (e.g. ``X-API-Key: abc123``
    or ``Authorization: Api-Token abc123``).
    """

    credential_type = ApiKeyCredential

    def build_url_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_connect_args(self, credential: Credential) -> dict[str, Any]:
        return {}

    def build_url_query_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_headers(self, credential: Credential) -> dict[str, str]:
        if not isinstance(credential, ApiKeyCredential):
            raise TypeError(
                f"Expected ApiKeyCredential, got {type(credential).__name__}"
            )
        return credential.to_headers()
