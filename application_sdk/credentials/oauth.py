"""OAuth 2.0 client_credentials token service.

Provides a general-purpose, asyncio-safe token service that wraps an
OAuthClientCredential and manages acquisition, caching, and refresh.

Usage::

    from application_sdk.credentials import OAuthClientCredential, OAuthTokenService

    cred = OAuthClientCredential(
        client_id="my-client",
        client_secret="secret",
        token_url="https://auth.example.com/token",
    )
    service = OAuthTokenService(cred)
    headers = await service.get_headers()   # {"Authorization": "Bearer <token>"}
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from application_sdk.credentials.types import OAuthClientCredential
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Refresh the token this many seconds before it actually expires.
_EXPIRY_BUFFER_SECONDS = 60


class OAuthTokenError(Exception):
    """Raised when an OAuth 2.0 token exchange fails."""


class OAuthTokenService:
    """General-purpose OAuth 2.0 client_credentials token service.

    Wraps an ``OAuthClientCredential`` and handles the full token lifecycle:
    acquire → cache → refresh before expiry.  Safe for concurrent use within
    a single asyncio event loop (an internal ``asyncio.Lock`` serialises
    exchange requests so only one HTTP call is in-flight at a time).

    This service is intentionally credential-source-agnostic: callers are
    responsible for constructing the ``OAuthClientCredential`` (from env vars,
    a secret store, the v3 credential resolver, etc.).

    Args:
        credential: Base credential supplying ``client_id``, ``client_secret``,
            ``token_url``, and optional ``scopes``.  The ``access_token`` and
            ``expires_at`` fields are managed internally and will be overwritten
            on the first exchange.
    """

    def __init__(self, credential: OAuthClientCredential) -> None:
        self._base = credential
        self._current = credential
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid access token, exchanging credentials if needed.

        Args:
            force_refresh: When ``True`` always performs a fresh token
                exchange, ignoring the cached token.

        Returns:
            A valid OAuth access token string.

        Raises:
            OAuthTokenError: If the exchange fails or the server returns
                no ``access_token``.
        """
        async with self._lock:
            if not force_refresh and not self._needs_refresh():
                return self._current.access_token
            self._current = await self._exchange()
            return self._current.access_token

    async def get_headers(self) -> dict[str, str]:
        """Return HTTP Authorization headers with a valid Bearer token.

        Returns an empty dict when ``client_id`` or ``client_secret`` are
        not set (auth not configured), so callers can always
        ``headers.update(await service.get_headers())`` unconditionally.
        """
        if not self._base.client_id or not self._base.client_secret:
            return {}
        token = await self.get_token()
        return {"Authorization": f"Bearer {token}"}

    @property
    def current_expires_at(self) -> datetime | None:
        """Expiry of the cached token as a timezone-aware UTC datetime.

        Returns ``None`` if no token has been acquired yet or the server
        did not return an ``expires_in`` field.
        """
        if not self._current.expires_at:
            return None
        try:
            expiry = datetime.fromisoformat(self._current.expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            return expiry
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _needs_refresh(self) -> bool:
        """True if there is no token or it expires within the buffer window."""
        if not self._current.access_token:
            return True
        expires_at = self.current_expires_at
        if expires_at is None:
            return False  # No expiry known — assume still valid
        return datetime.now(UTC) >= expires_at - timedelta(
            seconds=_EXPIRY_BUFFER_SECONDS
        )

    async def _exchange(self) -> OAuthClientCredential:
        """Perform a client_credentials HTTP token exchange.

        Returns:
            A new ``OAuthClientCredential`` with the acquired token and
            updated ``expires_at``.

        Raises:
            OAuthTokenError: On HTTP error or missing ``access_token``.
        """
        import httpx

        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self._base.client_id,
            "client_secret": self._base.client_secret,
        }
        if self._base.scopes:
            data["scope"] = " ".join(self._base.scopes)

        logger.debug(
            "Exchanging OAuth client credentials for %s at %s",
            self._base.client_id,
            self._base.token_url,
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as http:
                response = await http.post(self._base.token_url, data=data)
                response.raise_for_status()
                body: dict = response.json()
        except httpx.HTTPStatusError as exc:
            raise OAuthTokenError(
                f"Token exchange failed (HTTP {exc.response.status_code})"
            ) from exc
        except httpx.HTTPError as exc:
            raise OAuthTokenError(f"Token exchange HTTP error: {exc}") from exc

        access_token: str = body.get("access_token", "")
        if not access_token:
            error = body.get("error", "unknown")
            error_desc = body.get("error_description", "")
            raise OAuthTokenError(
                f"OAuth exchange returned no access_token: error={error}, description={error_desc}"
            )

        expires_in = body.get("expires_in")
        expires_at = ""
        if expires_in is not None:
            expires_at = (
                datetime.now(UTC) + timedelta(seconds=int(expires_in))
            ).isoformat()

        logger.debug("OAuth token acquired, expires_at=%s", expires_at or "unknown")
        return self._base.with_new_token(
            access_token=access_token,
            expires_at=expires_at,
        )
