"""Clock-skew calibration: a slow container clock must not delay token refresh.

Production failure mode: the SDR runs on a developer's macOS machine. When the
host sleeps, the Podman VM clock pauses while real time advances, so after wake
the container clock can be minutes behind. The OAuth server (correct clock)
issues ``exp = real_now + TTL``; computed against the slow local clock,
``remaining = exp - slow_now = TTL + skew``, so the refresh is scheduled after
the Temporal server has already rejected the token.

The fix: ``OAuthTokenService._exchange`` brackets the token call with local
``time.time()`` and reads the JWT ``iat`` (server clock), giving
``clock_offset_seconds = iat - local_midpoint``; ``_calculate_sleep_seconds``
then uses ``time.time() + offset`` as server-now.

This used to be an integration test that slept a real 35s and passed only if
the 30s refresh fired first. Here the calibrating exchange runs for real (only
the HTTP transport and both modules' clocks are stubbed) and the resulting
schedule is asserted directly — no waiting.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.execution._temporal.auth import (
    TemporalAuthConfig,
    TemporalAuthManager,
)

_REAL_NOW = 1_800_000_000  # server-correct wall clock (fixed)
_TOKEN_TTL = 60
_CLOCK_SKEW_SECONDS = 60  # container clock this many seconds behind real time


def _jwt_response() -> dict:
    """A token whose iat/exp come from the server's (correct) clock."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims = {"iat": _REAL_NOW, "exp": _REAL_NOW + _TOKEN_TTL}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return {
        "access_token": f"{header}.{payload.decode()}.sig",
        "expires_in": _TOKEN_TTL,
    }


@pytest.mark.asyncio
async def test_iat_clock_offset_corrects_skewed_refresh_timing() -> None:
    """With a 60s-slow local clock and a 60s token, the next refresh is
    scheduled at TTL/2 = 30s, not (TTL + skew)/2 = 60s."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(side_effect=_jwt_response)
    http = AsyncMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    http.post = AsyncMock(return_value=resp)

    manager = TemporalAuthManager(
        config=TemporalAuthConfig(
            client_id="test",
            client_secret="test",
            token_url="https://mock-oauth.example.com/token",
        )
    )

    def slow_time() -> float:
        return float(_REAL_NOW - _CLOCK_SKEW_SECONDS)

    with (
        patch("application_sdk.credentials.oauth.time") as cred_time,
        patch("application_sdk.execution._temporal.auth.time") as auth_time,
        patch("httpx.AsyncClient", return_value=http),
    ):
        cred_time.time.side_effect = slow_time
        auth_time.time.side_effect = slow_time

        await manager.acquire_initial_token()
        svc = manager._get_token_service()

        assert svc.clock_offset_seconds == pytest.approx(_CLOCK_SKEW_SECONDS)
        assert manager._calculate_sleep_seconds() == pytest.approx(_TOKEN_TTL / 2)
