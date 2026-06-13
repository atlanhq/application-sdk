"""Background token refresh for Temporal auth.

Manages OAuth token lifecycle for long-running Temporal worker connections:
1. Acquires an initial OAuth token via client credentials flow
2. Runs a background asyncio task that refreshes the token before expiry
3. Updates the Temporal client's api_key in-place when tokens are refreshed

Token exchange is delegated to ``credentials.oauth.OAuthTokenService``; this
module owns only the Temporal-specific lifecycle (background loop, client
api_key update, event emission).  Clock-offset calibration and drift
correction live in ``OAuthTokenService`` and are consumed here via the
``clock_offset_seconds`` property.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from application_sdk.constants import APPLICATION_NAME
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

    Clock-offset calibration is delegated to ``OAuthTokenService``.
    ``_calculate_sleep_seconds`` reads ``clock_offset_seconds`` from the
    service to correct for container clock drift without reinventing the
    calibration logic here.
    """

    config: TemporalAuthConfig
    _token_service: OAuthTokenService | None = field(default=None, repr=False)
    _refresh_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

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
            access_token = await self._get_token_service().get_token(force_refresh=True)
        except Exception as exc:
            from application_sdk.execution._temporal._activity_errors import (  # noqa: PLC0415
                TemporalAuthTokenAcquireError,
            )

            raise TemporalAuthTokenAcquireError(cause=exc) from exc

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
            except TimeoutError:  # conformance: ignore[E002] wait_for timeout = sleep interval elapsed; loop continues
                pass

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
                except TimeoutError:  # conformance: ignore[E002] wait_for timeout = retry duration elapsed; loop continues
                    pass

        logger.info("Token refresh loop exiting")

    async def _do_refresh(self, client: Client) -> None:
        """Perform a single token refresh and update the Temporal client."""
        access_token = await self._get_token_service().get_token(force_refresh=True)
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

            app_name = APPLICATION_NAME
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

        ``OAuthTokenService.current_expires_at`` stores the server-issued JWT
        exp claim (immune to container clock drift).
        ``OAuthTokenService.clock_offset_seconds`` is the iat-derived
        server–local offset calibrated on each exchange, so
        ``time.time() + offset ≈ server_now`` even after a host sleep/wake cycle.

        Negative remaining is clamped to ``_MIN_REFRESH_INTERVAL_SECONDS`` so
        the loop never spins against the token endpoint.
        """
        svc = self._get_token_service()
        expires_at = svc.current_expires_at
        if expires_at is None:
            return float(_MAX_REFRESH_INTERVAL_SECONDS)

        server_now = time.time() + svc.clock_offset_seconds
        remaining = expires_at.timestamp() - server_now

        if remaining <= 0:
            return float(_MIN_REFRESH_INTERVAL_SECONDS)

        sleep = remaining / 2
        return max(
            float(_MIN_REFRESH_INTERVAL_SECONDS),
            min(sleep, float(_MAX_REFRESH_INTERVAL_SECONDS)),
        )
