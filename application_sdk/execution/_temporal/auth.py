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
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger

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
            raise RuntimeError(
                f"Failed to acquire initial Temporal auth token: {exc}"
            ) from exc

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
                logger.warning("Token refresh task did not stop in time, cancelling")
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
            from application_sdk.credentials.oauth import OAuthTokenService
            from application_sdk.credentials.types import OAuthClientCredential

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
        raise ValueError(
            "Either token_url or base_url must be set in TemporalAuthConfig"
        )

    async def _refresh_loop(self, client: Client) -> None:
        """Background loop that refreshes tokens before expiry."""
        while not self._shutdown_event.is_set():
            sleep_seconds = self._calculate_sleep_seconds()

            expires_at = self._get_token_service().current_expires_at
            logger.debug(
                f"Token refresh sleeping {sleep_seconds:.0f}s, "
                f"expires_at={expires_at or 'unknown'}"
            )

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=sleep_seconds,
                )
                break
            except TimeoutError:
                pass

            try:
                await self._do_refresh(client)
            except Exception:
                logger.warning(
                    f"Token refresh failed, will retry in {_RETRY_INTERVAL_SECONDS}s",
                    exc_info=True,
                )
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=float(_RETRY_INTERVAL_SECONDS),
                    )
                    break
                except TimeoutError:
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
        """Emit a token_refresh lifecycle event via the EventBus (best-effort)."""
        import os
        import time

        try:
            from application_sdk.execution._temporal.interceptors.event_bus import (
                get_event_bus,
            )
            from application_sdk.execution._temporal.interceptors.events import (
                TOKEN_REFRESH,
                LifecycleEvent,
            )

            app_name = os.environ.get("ATLAN_APP_NAME", "")
            deployment_name = os.environ.get("ATLAN_DEPLOYMENT_NAME", app_name)
            now = time.time()
            expires_at_ts = expires_at.timestamp() if expires_at else 0.0
            remaining = max(0.0, expires_at_ts - now)

            event = LifecycleEvent(
                event_name=TOKEN_REFRESH,
                app_name=app_name,
                timestamp_ms=int(now * 1000),
                extra={
                    "force_refresh": "true",
                    "token_expiry_time": str(expires_at_ts),
                    "time_until_expiry": str(remaining),
                    "refresh_timestamp": str(now),
                    "application_name": app_name,
                    "deployment_name": deployment_name,
                },
            )
            await get_event_bus().emit(event)
        except Exception:
            logger.warning("Failed to emit token_refresh event", exc_info=True)

    def _calculate_sleep_seconds(self) -> float:
        """Calculate how long to sleep before next refresh."""
        expires_at = self._get_token_service().current_expires_at
        if expires_at is None:
            return float(_MAX_REFRESH_INTERVAL_SECONDS)

        now = datetime.now(UTC)
        remaining = (expires_at - now).total_seconds()

        if remaining <= 0:
            return 0

        sleep = remaining / 2
        return max(
            float(_MIN_REFRESH_INTERVAL_SECONDS),
            min(sleep, float(_MAX_REFRESH_INTERVAL_SECONDS)),
        )
