"""Credential Protocol and built-in typed credential dataclasses."""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol, runtime_checkable

# TODO(credentials): Add AwsCredential and migrate SQL client IAM auth to typed
# credentials to eliminate the v2 fallback entirely.
# TODO(credentials): Add GcsCredential, AdlsCredential if direct cloud credential
# handling is ever needed outside the obstore abstraction.


@runtime_checkable
class Credential(Protocol):
    """Protocol for typed credentials.

    All built-in and custom credential types implement this interface.
    """

    @property
    def credential_type(self) -> str:
        """Type identifier matching the registry key (e.g. 'api_key')."""
        ...

    async def validate(self) -> None:
        """Validate this credential.

        Raises:
            CredentialValidationError: If the credential is invalid.
        """
        ...


# ---------------------------------------------------------------------------
# Built-in credential types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BasicCredential:
    """Username + password credential."""

    username: str
    password: str

    @property
    def credential_type(self) -> str:
        return "basic"

    def to_auth_header(self) -> str:
        """Return a Basic Authorization header value (base64-encoded)."""
        import base64

        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return f"Basic {token}"

    async def validate(self) -> None:
        from application_sdk.credentials.errors import CredentialValidationError

        if not self.username:
            raise CredentialValidationError(
                "BasicCredential.username must not be empty",
                credential_name="basic",
            )
        if not self.password:
            raise CredentialValidationError(
                "BasicCredential.password must not be empty",
                credential_name="basic",
            )


@dataclasses.dataclass(frozen=True)
class ApiKeyCredential:
    """API key credential."""

    api_key: str
    header_name: str = "X-API-Key"
    prefix: str = ""

    @property
    def credential_type(self) -> str:
        return "api_key"

    def to_headers(self) -> dict[str, str]:
        """Return HTTP headers dict for this API key."""
        value = f"{self.prefix}{self.api_key}" if self.prefix else self.api_key
        return {self.header_name: value}

    async def validate(self) -> None:
        from application_sdk.credentials.errors import CredentialValidationError

        if not self.api_key:
            raise CredentialValidationError(
                "ApiKeyCredential.api_key must not be empty",
                credential_name="api_key",
            )


@dataclasses.dataclass(frozen=True)
class BearerTokenCredential:
    """Bearer token credential."""

    token: str
    expires_at: str = ""

    @property
    def credential_type(self) -> str:
        return "bearer_token"

    def to_auth_header(self) -> str:
        """Return a Bearer Authorization header value."""
        return f"Bearer {self.token}"

    def is_expired(self) -> bool:
        """Check whether the token has expired.

        Returns False if ``expires_at`` is empty (no expiry known).
        """
        if not self.expires_at:
            return False
        from datetime import UTC, datetime

        try:
            expiry = datetime.fromisoformat(self.expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            return datetime.now(UTC) >= expiry
        except ValueError:
            return False

    async def validate(self) -> None:
        from application_sdk.credentials.errors import CredentialValidationError

        if not self.token:
            raise CredentialValidationError(
                "BearerTokenCredential.token must not be empty",
                credential_name="bearer_token",
            )


@dataclasses.dataclass(frozen=True)
class OAuthClientCredential:
    """OAuth 2.0 client credential."""

    client_id: str
    client_secret: str
    token_url: str
    scopes: tuple[str, ...] = dataclasses.field(default_factory=tuple)
    access_token: str = ""
    refresh_token: str = ""
    expires_at: str = ""

    @property
    def credential_type(self) -> str:
        return "oauth_client"

    def needs_refresh(self) -> bool:
        """Return True if there is no valid access token."""
        if not self.access_token:
            return True
        if not self.expires_at:
            return False
        from datetime import UTC, datetime

        try:
            expiry = datetime.fromisoformat(self.expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            return datetime.now(UTC) >= expiry
        except ValueError:
            return False

    def with_new_token(
        self,
        access_token: str,
        expires_at: str = "",
        refresh_token: str = "",
    ) -> "OAuthClientCredential":
        """Return a new instance with updated token fields."""
        return dataclasses.replace(
            self,
            access_token=access_token,
            expires_at=expires_at,
            refresh_token=refresh_token or self.refresh_token,
        )

    def to_headers(self) -> dict[str, str]:
        """Return HTTP headers dict using the current access token."""
        return {"Authorization": f"Bearer {self.access_token}"}

    async def validate(self) -> None:
        from application_sdk.credentials.errors import CredentialValidationError

        if not self.client_id:
            raise CredentialValidationError(
                "OAuthClientCredential.client_id must not be empty",
                credential_name="oauth_client",
            )
        if not self.client_secret:
            raise CredentialValidationError(
                "OAuthClientCredential.client_secret must not be empty",
                credential_name="oauth_client",
            )
        if not self.token_url:
            raise CredentialValidationError(
                "OAuthClientCredential.token_url must not be empty",
                credential_name="oauth_client",
            )


@dataclasses.dataclass(frozen=True)
class CertificateCredential:
    """Certificate-based credential (mTLS / client cert)."""

    cert_data: str = ""
    """PEM-encoded certificate data."""

    key_data: str = ""
    """PEM-encoded private key data."""

    ca_data: str = ""
    """PEM-encoded CA bundle data."""

    passphrase: str = ""
    """Passphrase for the private key (empty if none)."""

    @property
    def credential_type(self) -> str:
        return "certificate"

    async def validate(self) -> None:
        from application_sdk.credentials.errors import CredentialValidationError

        if not self.cert_data and not self.key_data:
            raise CredentialValidationError(
                "CertificateCredential must have at least cert_data or key_data",
                credential_name="certificate",
            )


@dataclasses.dataclass(frozen=True)
class RawCredential:
    """Wrapper for raw dict credentials (legacy / unknown types).

    Used when the resolver encounters a credential_guid with
    credential_type="unknown" and no registered parser is available.
    """

    data: dict[str, Any]

    @property
    def credential_type(self) -> str:
        return "raw"

    async def validate(self) -> None:
        pass

    def get(self, key: str, default: Any = None) -> Any:
        """Get a field from the raw credential dict."""
        return self.data.get(key, default)


# ---------------------------------------------------------------------------
# Parser functions for built-in types
# ---------------------------------------------------------------------------


def _parse_basic(data: dict[str, Any]) -> BasicCredential:
    return BasicCredential(
        username=data.get("username", ""),
        password=data.get("password", ""),
    )


def _parse_api_key(data: dict[str, Any]) -> ApiKeyCredential:
    return ApiKeyCredential(
        api_key=data.get("api_key", ""),
        header_name=data.get("header_name", "X-API-Key"),
        prefix=data.get("prefix", ""),
    )


def _parse_bearer_token(data: dict[str, Any]) -> BearerTokenCredential:
    return BearerTokenCredential(
        token=data.get("token", ""),
        expires_at=data.get("expires_at", ""),
    )


def _parse_oauth_client(data: dict[str, Any]) -> OAuthClientCredential:
    scopes = data.get("scopes", [])
    return OAuthClientCredential(
        client_id=data.get("client_id", ""),
        client_secret=data.get("client_secret", ""),
        token_url=data.get("token_url", ""),
        scopes=tuple(scopes) if isinstance(scopes, list) else tuple(),
        access_token=data.get("access_token", ""),
        refresh_token=data.get("refresh_token", ""),
        expires_at=data.get("expires_at", ""),
    )


def _parse_certificate(data: dict[str, Any]) -> CertificateCredential:
    return CertificateCredential(
        cert_data=data.get("cert_data", ""),
        key_data=data.get("key_data", ""),
        ca_data=data.get("ca_data", ""),
        passphrase=data.get("passphrase", ""),
    )
