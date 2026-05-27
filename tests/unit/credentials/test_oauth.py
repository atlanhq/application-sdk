"""Unit tests for credentials.oauth.OAuthTokenService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.credentials.oauth import OAuthTokenError, OAuthTokenService
from application_sdk.credentials.types import OAuthClientCredential

# ---------------------------------------------------------------------------
# JWT helpers (shared)
# ---------------------------------------------------------------------------


def _cred(
    *,
    client_id: str = "cid",
    client_secret: str = "csecret",
    token_url: str = "https://auth.example.com/token",
    access_token: str = "",
    expires_at: str = "",
    scopes: tuple[str, ...] = (),
) -> OAuthClientCredential:
    return OAuthClientCredential(
        client_id=client_id,
        client_secret=client_secret,
        token_url=token_url,
        access_token=access_token,
        expires_at=expires_at,
        scopes=scopes,
    )


def _future_iso(seconds: int = 3600) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds: int = 10) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _mock_httpx_response(
    access_token: str = "tok123",
    expires_in: int = 3600,
    status_code: int = 200,
) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"access_token": access_token, "expires_in": expires_in}
    resp.status_code = status_code
    return resp


# ---------------------------------------------------------------------------
# get_token — basic acquisition
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_token_exchanges_when_no_token() -> None:
    svc = OAuthTokenService(_cred())
    mock_resp = _mock_httpx_response(access_token="fresh_tok")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        token = await svc.get_token()

    assert token == "fresh_tok"


@pytest.mark.anyio
async def test_get_token_uses_cache_when_valid() -> None:
    """No HTTP call when the cached token is still valid."""
    cred = _cred(access_token="cached_tok", expires_at=_future_iso(3600))
    svc = OAuthTokenService(cred)

    with patch("httpx.AsyncClient") as mock_client_cls:
        token = await svc.get_token()
        mock_client_cls.assert_not_called()

    assert token == "cached_tok"


@pytest.mark.anyio
async def test_get_token_refreshes_when_near_expiry() -> None:
    """Token within the 60-second buffer triggers a refresh."""
    cred = _cred(access_token="old_tok", expires_at=_future_iso(30))  # 30s left
    svc = OAuthTokenService(cred)
    mock_resp = _mock_httpx_response(access_token="new_tok")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        token = await svc.get_token()

    assert token == "new_tok"


@pytest.mark.anyio
async def test_get_token_refreshes_when_expired() -> None:
    cred = _cred(access_token="old_tok", expires_at=_past_iso(10))
    svc = OAuthTokenService(cred)
    mock_resp = _mock_httpx_response(access_token="renewed_tok")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        token = await svc.get_token()

    assert token == "renewed_tok"


@pytest.mark.anyio
async def test_force_refresh_ignores_valid_cache() -> None:
    cred = _cred(access_token="cached_tok", expires_at=_future_iso(3600))
    svc = OAuthTokenService(cred)
    mock_resp = _mock_httpx_response(access_token="forced_tok")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        token = await svc.get_token(force_refresh=True)

    assert token == "forced_tok"


# ---------------------------------------------------------------------------
# get_headers
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_headers_returns_bearer() -> None:
    cred = _cred(access_token="mytoken", expires_at=_future_iso(3600))
    svc = OAuthTokenService(cred)

    headers = await svc.get_headers()
    assert headers == {"Authorization": "Bearer mytoken"}


@pytest.mark.anyio
async def test_get_headers_empty_when_no_credentials() -> None:
    """No client_id → returns empty dict (auth not configured)."""
    svc = OAuthTokenService(_cred(client_id="", client_secret=""))
    headers = await svc.get_headers()
    assert headers == {}


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_scopes_sent_in_request() -> None:
    cred = _cred(scopes=("read", "write"))
    svc = OAuthTokenService(cred)
    mock_resp = _mock_httpx_response()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        await svc.get_token()

        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["data"]["scope"] == "read write"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_raises_oauth_token_error_on_missing_access_token() -> None:
    svc = OAuthTokenService(_cred())

    bad_resp = MagicMock()
    bad_resp.raise_for_status = MagicMock()
    bad_resp.json.return_value = {"error": "invalid_client"}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=bad_resp)
        mock_client_cls.return_value = mock_client

        with pytest.raises(OAuthTokenError, match="no access_token"):
            await svc.get_token()


@pytest.mark.anyio
async def test_raises_oauth_token_error_on_http_status_error() -> None:
    import httpx

    svc = OAuthTokenService(_cred())

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        err_response = MagicMock()
        err_response.status_code = 401
        err_response.text = "Unauthorized"
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "401", request=MagicMock(), response=err_response
            )
        )
        mock_client_cls.return_value = mock_client

        with pytest.raises(OAuthTokenError, match="HTTP 401"):
            await svc.get_token()


# ---------------------------------------------------------------------------
# current_expires_at property
# ---------------------------------------------------------------------------


def test_current_expires_at_none_when_no_token() -> None:
    svc = OAuthTokenService(_cred())
    assert svc.current_expires_at is None


def test_current_expires_at_none_when_no_expires_at() -> None:
    svc = OAuthTokenService(_cred(access_token="tok"))
    assert svc.current_expires_at is None


def test_current_expires_at_parsed_correctly() -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    cred = _cred(access_token="tok", expires_at=future.isoformat())
    svc = OAuthTokenService(cred)
    result = svc.current_expires_at
    assert result is not None
    assert abs((result - future).total_seconds()) < 1


def test_current_expires_at_returns_none_on_bad_iso() -> None:
    cred = _cred(access_token="tok", expires_at="not-a-date")
    svc = OAuthTokenService(cred)
    assert svc.current_expires_at is None


# ---------------------------------------------------------------------------
# _exchange — JWT exp used as expires_at
# ---------------------------------------------------------------------------


def _make_jwt_access_token(exp: float, iat: float | None = None) -> str:
    """Build a minimal fake JWT with the given exp (and optional iat) claims."""
    import base64
    import json

    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    claims: dict[str, object] = {"exp": exp}
    if iat is not None:
        claims["iat"] = iat
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    )
    return f"{header}.{payload}.sig"


# ---------------------------------------------------------------------------
# clock_offset_seconds / _calibrate_clock_offset
# ---------------------------------------------------------------------------


def test_clock_offset_seconds_defaults_to_zero() -> None:
    svc = OAuthTokenService(_cred())
    assert svc.clock_offset_seconds == 0.0


def test_calibrate_clock_offset_sets_offset_for_jwt() -> None:
    """Valid JWT iat → offset = iat - local_midpoint."""
    import base64
    import json

    server_iat = 1_700_000_000.0
    exp = server_iat + 600.0
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"iat": server_iat, "exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    jwt_token = f"{header}.{payload}.sig"

    svc = OAuthTokenService(_cred())
    # Container clock 30 s slow: local midpoint = server_iat - 30 → offset = +30
    svc._calibrate_clock_offset(
        jwt_token, t_before=server_iat - 30.5, t_after=server_iat - 29.5
    )

    assert svc.clock_offset_seconds == pytest.approx(30.0, abs=0.1)


def test_calibrate_clock_offset_skips_opaque_token() -> None:
    """Opaque (non-JWT) token leaves existing offset unchanged."""
    svc = OAuthTokenService(_cred())
    svc._clock_offset_seconds = 5.0

    svc._calibrate_clock_offset("opaque-token", t_before=0.0, t_after=0.0)

    assert svc.clock_offset_seconds == 5.0


def test_calibrate_clock_offset_skips_when_no_iat() -> None:
    """JWT without iat claim leaves existing offset unchanged."""
    import base64
    import json

    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": 1_700_000_600}).encode())
        .rstrip(b"=")
        .decode()
    )
    jwt_no_iat = f"{header}.{payload}.sig"

    svc = OAuthTokenService(_cred())
    svc._clock_offset_seconds = 3.0
    svc._calibrate_clock_offset(jwt_no_iat, t_before=0.0, t_after=0.0)

    assert svc.clock_offset_seconds == 3.0


@pytest.mark.anyio
async def test_exchange_calibrates_clock_offset_from_jwt_iat() -> None:
    """After _exchange, clock_offset_seconds reflects the JWT iat vs local time."""
    server_iat = 1_700_000_030.0
    jwt_token = _make_jwt_access_token(exp=server_iat + 600, iat=server_iat)

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"access_token": jwt_token, "expires_in": 600}

    with (
        patch("application_sdk.credentials.oauth.time") as mock_time,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        # Local clock is 30 s slow: time.time() = server_iat - 30
        mock_time.time.return_value = server_iat - 30.0

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        svc = OAuthTokenService(_cred())
        await svc.get_token(force_refresh=True)

    # iat=1_700_000_030, local_midpoint=1_700_000_000 → offset = +30
    assert svc.clock_offset_seconds == pytest.approx(30.0, abs=0.01)


# ---------------------------------------------------------------------------
# _needs_refresh with clock offset
# ---------------------------------------------------------------------------


def test_needs_refresh_applies_clock_offset() -> None:
    """_needs_refresh uses server_now = local_now + offset so a slow container
    clock does not cause it to hold onto a token the server already rejects."""
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    expires_at = fixed_now + timedelta(seconds=120)

    cred = _cred(access_token="tok", expires_at=expires_at.isoformat())
    svc = OAuthTokenService(cred)

    with patch("application_sdk.credentials.oauth.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.fromtimestamp = datetime.fromtimestamp

        # No offset: server_now = fixed_now → 120s remaining > 60s buffer → False
        svc._clock_offset_seconds = 0.0
        assert svc._needs_refresh() is False

        # Offset +70s: server_now = fixed_now + 70s → 50s remaining < 60s buffer → True
        svc._clock_offset_seconds = 70.0
        assert svc._needs_refresh() is True


@pytest.mark.anyio
async def test_exchange_uses_jwt_exp_for_expires_at() -> None:
    """When the token is a JWT, expires_at is taken from the server-issued exp
    claim rather than datetime.now(UTC)+expires_in, making it immune to local
    clock drift."""
    server_exp = 1_700_010_000.0
    jwt_token = _make_jwt_access_token(exp=server_exp)

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"access_token": jwt_token, "expires_in": 600}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        svc = OAuthTokenService(_cred())
        await svc.get_token(force_refresh=True)

    result = svc.current_expires_at
    assert result is not None
    assert result == datetime.fromtimestamp(server_exp, UTC)


@pytest.mark.anyio
async def test_exchange_falls_back_to_expires_in_for_opaque_token() -> None:
    """Opaque tokens (no JWT exp) fall back to the expires_in-based calculation."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"access_token": "opaque-token", "expires_in": 600}

    before = datetime.now(UTC)
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        svc = OAuthTokenService(_cred())
        await svc.get_token(force_refresh=True)
    after = datetime.now(UTC)

    result = svc.current_expires_at
    assert result is not None
    # Should be ~600 s from now, not the JWT exp
    assert before + timedelta(seconds=599) <= result <= after + timedelta(seconds=601)
