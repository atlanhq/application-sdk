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
import base64
import json
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from application_sdk.credentials.types import OAuthClientCredential
from application_sdk.errors.leaves import AuthError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Refresh the token this many seconds before it actually expires.
_EXPIRY_BUFFER_SECONDS = 60


def _parse_jwt_exp(token: str) -> float | None:
    """Extract the exp claim from a JWT payload, or None for opaque tokens.

    Uses the server-issued exp rather than locally-computed now()+expires_in so
    that current_expires_at is immune to container clock drift on sleep/wake.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims: dict[str, object] = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        return float(exp) if exp is not None else None  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return None


class OAuthTokenError(AuthError):
    """Raised when an OAuth 2.0 token exchange or refresh fails (category=AUTH)."""

    code: ClassVar[str] = "OAUTH_TOKEN"

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        AuthError.__init__(self, message=message, cause=cause)


class OAuthTokenService:
    """General-purpose OAuth 2.0 client_credentials token service.

    Wraps an ``OAuthClientCredential`` and handles the full token lifecycle:
    acquire → cache → refresh before expiry.  Safe for concurrent use within
    a single asyncio event loop (an internal ``asyncio.Lock`` serialises
    exchange requests so only one HTTP call is in-flight at a time).

    Clock-drift correction: each successful token exchange derives a
    server–local clock offset from the JWT ``iat`` claim via
    ``_calibrate_clock_offset``.  The offset is exposed as
    ``clock_offset_seconds`` so callers can compute a correct
    ``server_now = time.time() + svc.clock_offset_seconds`` without
    reinventing the calibration themselves.

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
        # Seconds by which the server clock leads the local clock (positive =
        # local is slow).  Derived from the JWT iat claim on each exchange.
        self._clock_offset_seconds: float = 0.0

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

    @property
    def clock_offset_seconds(self) -> float:
        """Server–local clock offset in seconds (positive = container clock is slow).

        Derived from the JWT ``iat`` claim on each successful token exchange.
        Use ``time.time() + svc.clock_offset_seconds`` to get a clock-corrected
        estimate of the server's current time.
        """
        return self._clock_offset_seconds

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _needs_refresh(self) -> bool:
        """True if there is no token or it expires within the buffer window.

        Uses the clock-offset-corrected server_now so that a slow container
        clock does not cause the service to hold onto an already-expired token.
        """
        if not self._current.access_token:
            return True
        expires_at = self.current_expires_at
        if expires_at is None:
            return False  # No expiry known — assume still valid
        server_now = datetime.now(UTC) + timedelta(seconds=self._clock_offset_seconds)
        return server_now >= expires_at - timedelta(seconds=_EXPIRY_BUFFER_SECONDS)

    def _calibrate_clock_offset(
        self, token: str, t_before: float, t_after: float
    ) -> None:
        """Derive the server–local clock offset from the JWT iat claim (best-effort).

        Compares the server-issued ``iat`` with the midpoint of the local
        timestamps that bracket the HTTP call.  A positive offset means the
        container clock is slow — the production failure mode on SDR after a
        host sleep/wake cycle.

        Opaque tokens (no three-part JWT structure) are silently skipped so
        the existing offset is preserved.
        """
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            claims: dict[str, object] = json.loads(base64.urlsafe_b64decode(padded))
            iat = claims.get("iat")
            if iat is not None:
                local_midpoint = (t_before + t_after) / 2
                self._clock_offset_seconds = float(iat) - local_midpoint  # type: ignore[arg-type]
                if abs(self._clock_offset_seconds) > 1:
                    logger.debug(
                        "Server clock offset: %.1fs (container clock is %s)",
                        abs(self._clock_offset_seconds),
                        "slow" if self._clock_offset_seconds > 0 else "fast",
                    )
        except Exception:
            logger.warning(
                "Non-JWT or malformed token; clock calibration values unchanged",
                exc_info=True,
            )

    async def _exchange(self) -> OAuthClientCredential:
        """Perform a client_credentials HTTP token exchange.

        Returns:
            A new ``OAuthClientCredential`` with the acquired token and
            updated ``expires_at``.

        Raises:
            OAuthTokenError: On HTTP error or missing ``access_token``.
        """
        import httpx  # deferred: matches existing lazy-import pattern for optional heavy deps  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules

        from application_sdk.clients.ssl_utils import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
            get_ssl_context,
        )

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

        t_before = time.time()
        try:
            async with httpx.AsyncClient(
                timeout=30.0, verify=get_ssl_context()
            ) as http:
                response = await http.post(self._base.token_url, data=data)
                response.raise_for_status()
                body: dict = response.json()
        except httpx.HTTPStatusError as exc:
            raise OAuthTokenError(
                message=f"Token exchange failed (HTTP {exc.response.status_code})",
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise OAuthTokenError(
                message=f"Token exchange HTTP error: {exc}", cause=exc
            ) from exc
        t_after = time.time()

        access_token: str = body.get("access_token", "")
        if not access_token:
            error = body.get("error", "unknown")
            error_desc = body.get("error_description", "")
            raise OAuthTokenError(
                message=f"OAuth exchange returned no access_token: error={error}, description={error_desc}"
            )

        expires_in = body.get("expires_in")
        exp_ts = _parse_jwt_exp(access_token)
        if exp_ts is not None:
            # Prefer the server-issued exp claim: it is set by the OAuth server
            # using the server's own clock and is immune to container clock drift
            # (e.g. after a VM sleep/wake cycle).
            expires_at = datetime.fromtimestamp(exp_ts, UTC).isoformat()
        elif expires_in is not None:
            expires_at = (
                datetime.now(UTC) + timedelta(seconds=int(expires_in))
            ).isoformat()
        else:
            expires_at = ""

        # Calibrate clock offset from JWT iat — best-effort, opaque tokens skipped.
        self._calibrate_clock_offset(access_token, t_before, t_after)

        logger.debug("OAuth token acquired, expires_at=%s", expires_at or "unknown")
        return self._base.with_new_token(
            access_token=access_token,
            expires_at=expires_at,
        )
