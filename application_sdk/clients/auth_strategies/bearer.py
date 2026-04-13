"""Bearer-token auth strategy."""

from __future__ import annotations

from typing import Any

from application_sdk.credentials.types import BearerTokenCredential, Credential


class BearerTokenAuthStrategy:
    """Handles BearerTokenCredential for REST API clients.

    Returns an ``Authorization: Bearer <token>`` header.
    """

    credential_type = BearerTokenCredential

    def build_url_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_connect_args(self, credential: Credential) -> dict[str, Any]:
        return {}

    def build_url_query_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_headers(self, credential: Credential) -> dict[str, str]:
        if not isinstance(credential, BearerTokenCredential):
            raise TypeError(
                f"Expected BearerTokenCredential, got {type(credential).__name__}"
            )
        return {"Authorization": credential.to_auth_header()}
