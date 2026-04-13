"""OAuth 2.0 client-credentials auth strategy."""

from __future__ import annotations

from typing import Any

from application_sdk.credentials.types import Credential, OAuthClientCredential


class OAuthAuthStrategy:
    """Handles OAuthClientCredential for SQL and REST clients.

    SQL: Returns the access token as a URL param and optionally sets
    ``authenticator=oauth`` via ``url_query_params``.
    REST: Returns a ``Bearer`` Authorization header.

    If the token needs refresh, callers should refresh via
    ``OAuthClientCredential.with_new_token()`` before passing to
    the strategy.

    Args:
        url_query_params: Extra query params to append when used with
            SQL clients (e.g. ``{"authenticator": "oauth"}``).
    """

    credential_type = OAuthClientCredential

    def __init__(self, url_query_params: dict[str, str] | None = None) -> None:
        self._url_query_params = url_query_params or {}

    def build_url_params(self, credential: Credential) -> dict[str, str]:
        assert isinstance(credential, OAuthClientCredential)
        return {"token": credential.access_token}

    def build_connect_args(self, credential: Credential) -> dict[str, Any]:
        return {}

    def build_url_query_params(self, credential: Credential) -> dict[str, str]:
        return dict(self._url_query_params)

    def build_headers(self, credential: Credential) -> dict[str, str]:
        assert isinstance(credential, OAuthClientCredential)
        return credential.to_headers()
