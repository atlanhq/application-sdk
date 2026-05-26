"""Background token refresh for Temporal auth.

Manages OAuth token lifecycle for long-running Temporal worker connections:
1. Acquires an initial OAuth token via client credentials flow
2. Runs a background asyncio task that refreshes the token before expiry
3. Updates the Temporal client's api_key in-place when tokens are refreshed

Token exchange is delegated to ``credentials.oauth.OAuthTokenService``; this
module owns only the Temporal-specific lifecycle (background loop, client
api_key update, event emission).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from temporalio.client import Client

    from application_sdk.credentials.oauth import OAuthTokenService

# Refresh timing constants
_MIN_REFRESH_INTERVAL_SECONDS = 30
_MAX_REFRESH_INTERVAL_SECONDS = 300
_RETRY_INTERVAL_SECONDS = 30


@dataclass
class TemporalAuthConfig:
    """Configuration for Temporal OAuth authentication."""

    client_id: str = ""
    """OAuth client ID."""

    client_secret: str = ""
    """OAuth client secret."""

    token_url: str = ""
    """OAuth token URL (optional — derived from base_url if empty)."""

    base_url: str = ""
    """Atlan base URL (used to derive token_url if not set)."""

    scopes: str = ""
    """Comma-separated OAuth scopes."""


@dataclass
class TemporalAuthManager:
    """Manages OAuth token lifecycle for Temporal client connections.

    Acquires initial tokens and runs background refresh to keep
    long-running workers connected.  Token exchange is handled by an
    internal ``OAuthTokenService``; this class owns the Temporal-specific
    concerns (background loop timing, ``client.api_key`` update).
    """

    config: TemporalAuthConfig
    _token_service: OAuthTokenService | None = field(default=None, repr=False)
    _refresh_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Seconds by which the server clock leads the local clock (positive = local is
    # slow).  Derived from the JWT iat claim on each token receipt.
    _server_clock_offset_seconds: float = field(default=0.0, repr=False)
    # JWT exp claim from the most-recently-issued token (Unix timestamp, server
    # time).  Used in _calculate_sleep_seconds as the primary expiry source so
    # that scheduling is always relative to the server's clock, not the locally-
    # derived OAuthTokenService.current_expires_at (which is computed as
    # datetime.now(UTC) + expires_in and will be wrong when the container clock
    # has drifted).
    _server_token_exp: float | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire_initial_token(self) -> str:
        """Acquire the initial OAuth token via client credentials flow.

        Returns:
            The access_token string for Temporal client auth.

        Raises:
            RuntimeError: If token acquisition fails.
        """
        token_url = self._resolve_token_url()

        logger.info(
            "Acquiring initial Temporal auth token: client_id=%s token_url=%s",
            self.config.client_id,
            token_url,
        )

        try:
            t_before = time.time()
            access_token = await self._get_token_service().get_token(force_refresh=True)
            t_after = time.time()
        except Exception as exc:
            from application_sdk.execution._temporal._activity_errors import (  # noqa: PLC0415
                TemporalAuthTokenAcquireError,
            )

            raise TemporalAuthTokenAcquireError(cause=exc) from exc

        self._update_clock_offset(access_token, t_before, t_after)

        expires_at = self._get_token_service().current_expires_at
        logger.info(
            "Initial Temporal auth token acquired (expires_at=%s)",
            expires_at or "unknown",
        )
        return access_token

    def start_background_refresh(self, client: Client) -> None:
        """Start the background token refresh task.

        Args:
            client: The Temporal client whose api_key to update on refresh.
        """
        if self._refresh_task is not None:
            logger.warning(
                "Background token refresh already running, not starting another"
            )
            return

        self._refresh_task = asyncio.create_task(
            self._refresh_loop(client),
            name="temporal-auth-refresh",
        )
        logger.info("Background token refresh task started")

    async def shutdown(self) -> None:
        """Shut down the background refresh task."""
        self._shutdown_event.set()

        if self._refresh_task is not None:
            try:
                await asyncio.wait_for(self._refresh_task, timeout=5.0)
            except TimeoutError:
                logger.warning(
                    "Token refresh task did not stop in time, cancelling", exc_info=True
                )
                self._refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._refresh_task
            self._refresh_task = None

        logger.info("Auth manager shut down")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_token_service(self) -> OAuthTokenService:
        """Lazily construct the OAuthTokenService from TemporalAuthConfig."""
        if self._token_service is None:
            from application_sdk.credentials.oauth import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
                OAuthTokenService,
            )
            from application_sdk.credentials.types import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
                OAuthClientCredential,
            )

            cred = OAuthClientCredential(
                client_id=self.config.client_id,
                client_secret=self.config.client_secret,
                token_url=self._resolve_token_url(),
                scopes=tuple(
                    s.strip() for s in self.config.scopes.split(",") if s.strip()
                ),
            )
            self._token_service = OAuthTokenService(cred)
        return self._token_service

    def _resolve_token_url(self) -> str:
        """Derive token URL from config."""
        if self.config.token_url:
            return self.config.token_url
        if self.config.base_url:
            base = self.config.base_url.rstrip("/")
            return f"{base}/auth/realms/default/protocol/openid-connect/token"
        from application_sdk.execution._temporal._activity_errors import (  # noqa: PLC0415
            TemporalAuthConfigError,
        )

        raise TemporalAuthConfigError(
            message="Either token_url or base_url must be set", field="token_url"
        )

    def _update_clock_offset(self, token: str, t_before: float, t_after: float) -> None:
        """Derive the server–local clock offset and expiry from JWT claims.

        iat is set by the OAuth server at issue time using the server's
        (correct) clock.  Comparing it to the midpoint of the local timestamps
        bracketing the token request gives a first-order estimate of how far
        ahead the server clock is relative to the container clock.

        exp is the server-time expiry.  Storing it separately (as
        _server_token_exp) lets _calculate_sleep_seconds schedule refreshes
        relative to the server's clock rather than the locally-derived
        OAuthTokenService.current_expires_at (which is computed as
        datetime.now(UTC) + expires_in and is wrong when the clock has
        drifted).

        A positive offset means the container clock is slow (the production
        failure mode caused by VM pause/wake on SDR deployments).
        """
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return
            # base64url-decode the payload — no signature verification needed
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            claims: dict[str, object] = json.loads(base64.urlsafe_b64decode(padded))
            iat = claims.get("iat")
            if iat is not None:
                local_midpoint = (t_before + t_after) / 2
                self._server_clock_offset_seconds = float(iat) - local_midpoint  # type: ignore[arg-type]
                if abs(self._server_clock_offset_seconds) > 1:
                    logger.debug(
                        "Server clock offset: %.1fs (container clock is %s)",
                        abs(self._server_clock_offset_seconds),
                        "slow" if self._server_clock_offset_seconds > 0 else "fast",
                    )
            exp = claims.get("exp")
            if exp is not None:
                self._server_token_exp = float(exp)  # type: ignore[arg-type]
        except Exception:
            logger.warning(
                "Non-JWT or malformed token; clock calibration values unchanged",
                exc_info=True,
            )

    async def _refresh_loop(self, client: Client) -> None:
        """Background loop that refreshes tokens before expiry."""
        while not self._shutdown_event.is_set():
            sleep_seconds = self._calculate_sleep_seconds()

            expires_at = self._get_token_service().current_expires_at
            logger.debug(
                "Token refresh sleeping %.0fs, expires_at=%s",
                sleep_seconds,
                expires_at or "unknown",
            )

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=sleep_seconds,
                )
                break
            except TimeoutError:
                pass  # wait_for timeout means sleep duration elapsed; continue loop

            try:
                await self._do_refresh(client)
            except Exception:
                logger.warning(
                    "Token refresh failed, will retry in %ss",
                    _RETRY_INTERVAL_SECONDS,
                    exc_info=True,
                )
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=float(_RETRY_INTERVAL_SECONDS),
                    )
                    break
                except TimeoutError:
                    pass  # wait_for timeout means retry sleep elapsed; continue loop

        logger.info("Token refresh loop exiting")

    async def _do_refresh(self, client: Client) -> None:
        """Perform a single token refresh and update the Temporal client."""
        t_before = time.time()
        access_token = await self._get_token_service().get_token(force_refresh=True)
        t_after = time.time()

        self._update_clock_offset(access_token, t_before, t_after)

        expires_at = self._get_token_service().current_expires_at

        client.api_key = access_token

        logger.info(
            "Temporal auth token refreshed (expires_at=%s)", expires_at or "unknown"
        )

        await self._emit_token_refresh_event(expires_at)

    async def _emit_token_refresh_event(self, expires_at: datetime | None) -> None:
        """Emit a token_refresh lifecycle event via the event binding (best-effort)."""

        try:
            from application_sdk.contracts.events import (  # noqa: PLC0415 — circular: contracts.events imports execution.errors
                ApplicationEventNames,
                Event,
                EventTypes,
            )
            from application_sdk.execution._temporal.interceptors.events import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
                _publish_event_via_binding,
            )

            app_name = os.environ.get("ATLAN_APP_NAME", "")
            deployment_name = os.environ.get("ATLAN_DEPLOYMENT_NAME", app_name)
            now = time.time()
            expires_at_ts = expires_at.timestamp() if expires_at else 0.0
            remaining = max(0.0, expires_at_ts - now)

            event = Event(
                event_type=EventTypes.APPLICATION_EVENT.value,
                event_name=ApplicationEventNames.TOKEN_REFRESH.value,
                data={
                    "force_refresh": "true",
                    "token_expiry_time": str(expires_at_ts),
                    "time_until_expiry": str(remaining),
                    "refresh_timestamp": str(now),
                    "application_name": app_name,
                    "deployment_name": deployment_name,
                },
            )
            await _publish_event_via_binding(event)
        except Exception:
            logger.warning("Failed to emit token_refresh event", exc_info=True)

    def _calculate_sleep_seconds(self) -> float:
        """Calculate how long to sleep before next refresh.

        Prefers the JWT exp claim (_server_token_exp) as the expiry source
        because it is set by the OAuth server using the server's own clock and
        is therefore immune to container clock drift.

        OAuthTokenService.current_expires_at is derived as
        datetime.now(UTC) + expires_in using the *local* clock.  When the
        container clock has drifted (e.g. after a VM sleep/wake cycle on an
        SDR deployment), that value can be in the past even for a freshly-
        issued token — causing remaining to be negative and the refresh loop
        to spin.  Using exp directly side-steps that entirely.

        Falls back to current_expires_at (with offset correction) for opaque
        tokens that do not carry exp.  In both paths, negative remaining is
        clamped to _MIN_REFRESH_INTERVAL_SECONDS so the loop never spins.
        """
        if self._server_token_exp is not None:
            server_now = time.time() + self._server_clock_offset_seconds
            remaining = self._server_token_exp - server_now
        else:
            expires_at = self._get_token_service().current_expires_at
            if expires_at is None:
                return float(_MAX_REFRESH_INTERVAL_SECONDS)

            now = datetime.now(UTC)
            if self._server_clock_offset_seconds:
                now = now + timedelta(seconds=self._server_clock_offset_seconds)
            remaining = (expires_at - now).total_seconds()

        if remaining <= 0:
            return float(_MIN_REFRESH_INTERVAL_SECONDS)

        sleep = remaining / 2
        return max(
            float(_MIN_REFRESH_INTERVAL_SECONDS),
            min(sleep, float(_MAX_REFRESH_INTERVAL_SECONDS)),
        )
