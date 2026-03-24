"""Atlan-specific credential types."""

from __future__ import annotations

import os
from typing import Any

from pydantic import ConfigDict

from application_sdk.credentials.types import (
    BearerTokenCredential,
    OAuthClientCredential,
)


class AtlanApiToken(BearerTokenCredential, frozen=True):
    """Atlan API token credential.

    Extends ``BearerTokenCredential`` with the Atlan instance base URL.
    """

    model_config = ConfigDict(frozen=True)

    base_url: str = ""
    """URL of the Atlan instance (e.g. ``https://tenant.atlan.com``)."""

    @property
    def credential_type(self) -> str:
        return "atlan_api_token"

    async def validate(self) -> None:
        from application_sdk.credentials.errors import CredentialValidationError

        if not self.token:
            raise CredentialValidationError(
                "AtlanApiToken.token must not be empty",
                credential_name="atlan_api_token",
            )
        if not self.base_url:
            raise CredentialValidationError(
                "AtlanApiToken.base_url must not be empty",
                credential_name="atlan_api_token",
            )
        # Lazy import — avoids hard dependency for apps that don't use Atlan creds.
        from pyatlan_v9.client.aio import AsyncAtlanClient  # type: ignore[import]

        client = AsyncAtlanClient(base_url=self.base_url, api_key=self.token)
        try:
            await client.user.get_current()
        except Exception as exc:
            raise CredentialValidationError(
                f"AtlanApiToken validation failed: {exc}",
                credential_name="atlan_api_token",
                cause=exc,
            ) from exc


class AtlanOAuthClient(OAuthClientCredential, frozen=True):
    """Atlan OAuth 2.0 client credential.

    Extends ``OAuthClientCredential`` with the Atlan instance base URL.
    ``token_url`` is optional — when empty it is derived from ``base_url``.
    """

    model_config = ConfigDict(frozen=True)

    base_url: str = ""
    """URL of the Atlan instance (e.g. ``https://tenant.atlan.com``)."""

    @property
    def credential_type(self) -> str:
        return "atlan_oauth_client"

    @property
    def effective_token_url(self) -> str:
        """Derive the token URL from base_url when token_url is not set.

        Supports INTERNAL K8s routing via the ``ATLAN_INTERNAL_HERACLES_URL``
        environment variable.
        """
        if self.token_url:
            return self.token_url
        internal_url = os.environ.get("ATLAN_INTERNAL_HERACLES_URL", "")
        if internal_url:
            return f"{internal_url.rstrip('/')}/oauth/token"
        if self.base_url:
            return f"{self.base_url.rstrip('/')}/auth/realms/default/protocol/openid-connect/token"
        return ""

    def build_token_request_data(self) -> tuple[dict[str, Any], bool]:
        """Build the token request payload.

        Returns:
            A ``(data_dict, use_json)`` tuple:

            - Keycloak endpoint: form-encoded (use_json=False)
            - Heracles endpoint: JSON with camelCase fields (use_json=True)
        """
        internal_url = os.environ.get("ATLAN_INTERNAL_HERACLES_URL", "")
        if internal_url:
            # Heracles uses JSON + camelCase
            data: dict[str, Any] = {
                "clientId": self.client_id,
                "clientSecret": self.client_secret,
                "grantType": "client_credentials",
            }
            if self.scopes:
                data["scope"] = " ".join(self.scopes)
            return data, True
        # Keycloak uses form-encoded
        form: dict[str, Any] = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        if self.scopes:
            form["scope"] = " ".join(self.scopes)
        return form, False

    def with_new_token(
        self,
        access_token: str,
        expires_at: str = "",
        refresh_token: str = "",
    ) -> "AtlanOAuthClient":
        """Return a new instance with updated token fields, preserving AtlanOAuthClient type."""
        return self.model_copy(
            update={
                "access_token": access_token,
                "expires_at": expires_at,
                "refresh_token": refresh_token or self.refresh_token,
            }
        )

    async def validate(self) -> None:
        from application_sdk.credentials.errors import CredentialValidationError

        if not self.client_id:
            raise CredentialValidationError(
                "AtlanOAuthClient.client_id must not be empty",
                credential_name="atlan_oauth_client",
            )
        if not self.client_secret:
            raise CredentialValidationError(
                "AtlanOAuthClient.client_secret must not be empty",
                credential_name="atlan_oauth_client",
            )
        if not self.base_url:
            raise CredentialValidationError(
                "AtlanOAuthClient.base_url must not be empty",
                credential_name="atlan_oauth_client",
            )
        if not self.access_token:
            raise CredentialValidationError(
                "AtlanOAuthClient.access_token must not be empty (obtain a token first)",
                credential_name="atlan_oauth_client",
            )
        # Lazy import — avoids hard dependency for apps that don't use Atlan creds.
        from pyatlan_v9.client.aio import AsyncAtlanClient  # type: ignore[import]

        client = AsyncAtlanClient(base_url=self.base_url, api_key=self.access_token)
        try:
            await client.user.get_current()
        except Exception as exc:
            raise CredentialValidationError(
                f"AtlanOAuthClient validation failed: {exc}",
                credential_name="atlan_oauth_client",
                cause=exc,
            ) from exc


# ---------------------------------------------------------------------------
# Parser functions
# ---------------------------------------------------------------------------


def _parse_atlan_api_token(data: dict[str, Any]) -> AtlanApiToken:
    return AtlanApiToken(
        token=data.get("token", ""),
        expires_at=data.get("expires_at", ""),
        base_url=data.get("base_url", ""),
    )


def _parse_atlan_oauth_client(data: dict[str, Any]) -> AtlanOAuthClient:
    scopes = data.get("scopes", [])
    return AtlanOAuthClient(
        client_id=data.get("client_id", ""),
        client_secret=data.get("client_secret", ""),
        token_url=data.get("token_url", ""),
        scopes=tuple(scopes) if isinstance(scopes, list) else tuple(),
        access_token=data.get("access_token", ""),
        refresh_token=data.get("refresh_token", ""),
        expires_at=data.get("expires_at", ""),
        base_url=data.get("base_url", ""),
    )
