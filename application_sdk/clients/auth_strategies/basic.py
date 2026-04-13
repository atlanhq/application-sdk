"""Basic (username/password) auth strategy."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from application_sdk.credentials.types import BasicCredential, Credential


class BasicAuthStrategy:
    """Handles BasicCredential for SQL and REST clients.

    SQL: URL-encodes the password into the connection-string template.
    REST: Returns a ``Basic`` Authorization header.
    """

    credential_type = BasicCredential

    def build_url_params(self, credential: Credential) -> dict[str, str]:
        if not isinstance(credential, BasicCredential):
            raise TypeError(
                f"Expected BasicCredential, got {type(credential).__name__}"
            )
        return {"password": quote_plus(credential.password)}

    def build_connect_args(self, credential: Credential) -> dict[str, Any]:
        return {}

    def build_url_query_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_headers(self, credential: Credential) -> dict[str, str]:
        if not isinstance(credential, BasicCredential):
            raise TypeError(
                f"Expected BasicCredential, got {type(credential).__name__}"
            )
        return {"Authorization": credential.to_auth_header()}
