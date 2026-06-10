"""Tests for TemporalAuthManager.

This file deliberately exercises every inline import in
``application_sdk.execution._temporal.auth`` through a real call path so
that a renamed/removed symbol surfaces as a test failure rather than at
runtime in production.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest import mock

import pytest

from application_sdk.contracts.events import ApplicationEventNames, EventTypes
from application_sdk.execution._temporal._activity_errors import (
    TemporalAuthConfigError,
    TemporalAuthTokenAcquireError,
)
from application_sdk.execution._temporal.auth import (
    TemporalAuthConfig,
    TemporalAuthManager,
)

# The method imports _publish_event_via_binding locally, so we mock at its
# source module rather than on the auth module.
_PUBLISH_TARGET = (
    "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding"
)
# These are imported inline inside _get_token_service. Patching them at
# the SOURCE module ensures the inline import statement actually executes
# during the test — a renamed/removed symbol will fail at patch setup.
_OAUTH_SERVICE_TARGET = "application_sdk.credentials.oauth.OAuthTokenService"
_OAUTH_CRED_TARGET = "application_sdk.credentials.types.OAuthClientCredential"


def _make_manager(
    *,
    client_id: str = "test-client",
    client_secret: str = "test-secret",
    token_url: str = "https://example.com/token",
    base_url: str = "",
    scopes: str = "",
) -> TemporalAuthManager:
    """Create a TemporalAuthManager with dummy config for testing."""
    return TemporalAuthManager(
        config=TemporalAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
            base_url=base_url,
            scopes=scopes,
        )
    )


def _stub_token_service(
    manager: TemporalAuthManager,
    *,
    token: str = "stub-token",
    expires_at: datetime | None = None,
    clock_offset_seconds: float = 0.0,
) -> mock.MagicMock:
    """Inject a fully-stubbed OAuthTokenService onto the manager.

    Use this when a test does NOT need to exercise the inline imports in
    ``_get_token_service`` (other tests cover those explicitly).
    """
    svc = mock.MagicMock(name="OAuthTokenService")
    svc.get_token = mock.AsyncMock(return_value=token)
    svc.current_expires_at = expires_at
    svc.clock_offset_seconds = clock_offset_seconds
    manager._token_service = svc
    return svc


# ---------------------------------------------------------------------------
# Existing _emit_token_refresh_event tests (kept).
# ---------------------------------------------------------------------------


class TestEmitTokenRefreshEvent:
    """Tests for TemporalAuthManager._emit_token_refresh_event."""

    @pytest.mark.asyncio
    async def test_publishes_event_successfully(self) -> None:
        """_emit_token_refresh_event calls _publish_event_via_binding with correct event."""
        manager = _make_manager()
        expires_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        with mock.patch(_PUBLISH_TARGET) as mock_publish:
            mock_publish.return_value = None

            await manager._emit_token_refresh_event(expires_at)

            mock_publish.assert_called_once()
            event = mock_publish.call_args[0][0]

            assert event.event_type == EventTypes.APPLICATION_EVENT.value
            assert event.event_name == ApplicationEventNames.TOKEN_REFRESH.value

    @pytest.mark.asyncio
    async def test_does_not_raise_on_publish_failure(self) -> None:
        """_emit_token_refresh_event swallows exceptions from publishing."""
        manager = _make_manager()
        expires_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        with mock.patch(
            _PUBLISH_TARGET,
            side_effect=RuntimeError("binding unavailable"),
        ):
            # Should not raise
            await manager._emit_token_refresh_event(expires_at)

    @pytest.mark.asyncio
    async def test_does_not_raise_on_import_failure(self) -> None:
        """_emit_token_refresh_event handles import errors gracefully."""
        manager = _make_manager()

        # Simulate the contracts import failing inside the try block
        with mock.patch.dict(
            "sys.modules",
            {"application_sdk.contracts.events": None},
        ):
            # Should not raise
            await manager._emit_token_refresh_event(None)

    @pytest.mark.asyncio
    async def test_event_metadata_contains_expected_fields(self) -> None:
        """Event data includes deployment_name, application_name, and timing fields."""
        manager = _make_manager()
        expires_at = datetime(2026, 6, 15, 10, 30, 0, tzinfo=UTC)

        with (
            mock.patch(
                "application_sdk.execution._temporal.auth.APPLICATION_NAME",
                "my-test-app",
            ),
            mock.patch.dict(
                "os.environ", {"ATLAN_DEPLOYMENT_NAME": "my-deployment"}, clear=False
            ),
            mock.patch(_PUBLISH_TARGET) as mock_publish,
        ):
            mock_publish.return_value = None

            await manager._emit_token_refresh_event(expires_at)

            event = mock_publish.call_args[0][0]
            data = event.data

            assert data["application_name"] == "my-test-app"
            assert data["deployment_name"] == "my-deployment"
            assert data["force_refresh"] == "true"
            assert "token_expiry_time" in data
            assert "time_until_expiry" in data
            assert "refresh_timestamp" in data

    @pytest.mark.asyncio
    async def test_deployment_name_falls_back_to_app_name(self) -> None:
        """When ATLAN_DEPLOYMENT_NAME is unset, deployment_name falls back to app_name."""
        manager = _make_manager()
        expires_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        import os

        with (
            mock.patch(
                "application_sdk.execution._temporal.auth.APPLICATION_NAME",
                "fallback-app",
            ),
            mock.patch(_PUBLISH_TARGET) as mock_publish,
        ):
            os.environ.pop("ATLAN_DEPLOYMENT_NAME", None)
            mock_publish.return_value = None

            await manager._emit_token_refresh_event(expires_at)

            event = mock_publish.call_args[0][0]
            assert event.data["deployment_name"] == "fallback-app"

    @pytest.mark.asyncio
    async def test_handles_none_expires_at(self) -> None:
        """When expires_at is None, event still publishes with zero timestamps."""
        manager = _make_manager()

        with mock.patch(_PUBLISH_TARGET) as mock_publish:
            mock_publish.return_value = None

            await manager._emit_token_refresh_event(None)

            mock_publish.assert_called_once()
            event = mock_publish.call_args[0][0]
            assert event.data["token_expiry_time"] == "0.0"

    @pytest.mark.asyncio
    async def test_uses_correct_event_type_and_name(self) -> None:
        """Verify the exact event_type and event_name string values."""
        manager = _make_manager()

        with mock.patch(_PUBLISH_TARGET) as mock_publish:
            mock_publish.return_value = None

            await manager._emit_token_refresh_event(datetime(2026, 1, 1, tzinfo=UTC))

            event = mock_publish.call_args[0][0]
            assert event.event_type == "application_events"
            assert event.event_name == "token_refresh"


# ---------------------------------------------------------------------------
# Inline-import contract tests.
# ---------------------------------------------------------------------------


class TestInlineImportContracts:
    """Pin the inline imports in auth.py to their real source modules."""

    def test_oauth_token_service_symbol_exists(self) -> None:
        """`OAuthTokenService` must exist on application_sdk.credentials.oauth."""
        # This is a guard: if the symbol is renamed, mock.patch raises
        # AttributeError during setup and this test fails loudly.
        with mock.patch(_OAUTH_SERVICE_TARGET):
            pass

    def test_oauth_client_credential_symbol_exists(self) -> None:
        """`OAuthClientCredential` must exist on application_sdk.credentials.types."""
        with mock.patch(_OAUTH_CRED_TARGET):
            pass

    def test_publish_event_via_binding_symbol_exists(self) -> None:
        """`_publish_event_via_binding` must exist at its source module."""
        with mock.patch(_PUBLISH_TARGET):
            pass

    def test_contracts_events_symbols_exist(self) -> None:
        """Event/EventTypes/ApplicationEventNames must be importable."""
        # Direct import — failing here is a renamed/removed symbol.
        from application_sdk.contracts.events import (  # noqa: F401
            ApplicationEventNames,
            Event,
            EventTypes,
        )

    def test_get_token_service_runs_inline_imports(self) -> None:
        """_get_token_service exercises both inline imports on first call.

        Patches the symbols at their SOURCE modules. The inline `from ...
        import` statements in auth.py resolve to the (patched) attribute
        on the source module — so the import statement itself runs. If
        either symbol is renamed, mock.patch raises AttributeError at
        setup time.
        """
        manager = _make_manager(scopes="read,write")

        with (
            mock.patch(_OAUTH_SERVICE_TARGET) as svc_cls,
            mock.patch(_OAUTH_CRED_TARGET) as cred_cls,
        ):
            svc_instance = mock.MagicMock(name="OAuthTokenService instance")
            svc_cls.return_value = svc_instance

            result = manager._get_token_service()

            assert result is svc_instance
            cred_cls.assert_called_once()
            svc_cls.assert_called_once()


# ---------------------------------------------------------------------------
# _resolve_token_url
# ---------------------------------------------------------------------------


class TestResolveTokenUrl:
    def test_explicit_token_url_wins(self) -> None:
        manager = _make_manager(token_url="https://auth.example.com/token")
        assert manager._resolve_token_url() == "https://auth.example.com/token"

    def test_derives_from_base_url(self) -> None:
        manager = _make_manager(token_url="", base_url="https://atlan.example.com")
        assert (
            manager._resolve_token_url()
            == "https://atlan.example.com/auth/realms/default/protocol/openid-connect/token"
        )

    def test_strips_trailing_slash_on_base_url(self) -> None:
        manager = _make_manager(token_url="", base_url="https://atlan.example.com/")
        assert manager._resolve_token_url().startswith(
            "https://atlan.example.com/auth/"
        )
        assert "//auth/" not in manager._resolve_token_url()

    def test_raises_when_neither_set(self) -> None:
        manager = _make_manager(token_url="", base_url="")
        with pytest.raises(TemporalAuthConfigError):
            manager._resolve_token_url()

    # -- https enforcement --------------------------------------------------

    def test_plain_http_token_url_rejected(self) -> None:
        manager = _make_manager(token_url="http://auth.example.com/token")
        with pytest.raises(TemporalAuthConfigError) as exc_info:
            manager._resolve_token_url()
        assert "https" in str(exc_info.value)

    def test_plain_http_base_url_rejected(self) -> None:
        manager = _make_manager(token_url="", base_url="http://atlan.example.com")
        with pytest.raises(TemporalAuthConfigError):
            manager._resolve_token_url()

    def test_http_localhost_token_url_allowed(self) -> None:
        manager = _make_manager(token_url="http://localhost:8080/token")
        assert manager._resolve_token_url() == "http://localhost:8080/token"

    def test_http_loopback_base_url_allowed(self) -> None:
        manager = _make_manager(token_url="", base_url="http://127.0.0.1:8080")
        assert manager._resolve_token_url().startswith("http://127.0.0.1:8080/auth/")

    def test_schemeless_token_url_rejected(self) -> None:
        manager = _make_manager(token_url="auth.example.com/token")
        with pytest.raises(TemporalAuthConfigError):
            manager._resolve_token_url()


# ---------------------------------------------------------------------------
# _get_token_service
# ---------------------------------------------------------------------------


class TestGetTokenService:
    def test_lazy_construction_caches_service(self) -> None:
        """Repeated calls return the same instance — one OAuthTokenService."""
        manager = _make_manager()
        with (
            mock.patch(_OAUTH_SERVICE_TARGET) as svc_cls,
            mock.patch(_OAUTH_CRED_TARGET),
        ):
            svc_cls.return_value = mock.MagicMock()

            first = manager._get_token_service()
            second = manager._get_token_service()

            assert first is second
            assert svc_cls.call_count == 1

    def test_parses_scopes_from_comma_string(self) -> None:
        """Scopes are split on commas, stripped of whitespace, and emptys dropped."""
        manager = _make_manager(scopes="read , write,, , admin ")
        with (
            mock.patch(_OAUTH_SERVICE_TARGET),
            mock.patch(_OAUTH_CRED_TARGET) as cred_cls,
        ):
            manager._get_token_service()
            kwargs = cred_cls.call_args.kwargs
            assert kwargs["scopes"] == ("read", "write", "admin")

    def test_passes_credentials_to_oauth_client_credential(self) -> None:
        manager = _make_manager(
            client_id="abc",
            client_secret="xyz",
            token_url="https://t.example.com/token",
            scopes="s1",
        )
        with (
            mock.patch(_OAUTH_SERVICE_TARGET),
            mock.patch(_OAUTH_CRED_TARGET) as cred_cls,
        ):
            manager._get_token_service()
            kwargs = cred_cls.call_args.kwargs
            assert kwargs["client_id"] == "abc"
            assert kwargs["client_secret"] == "xyz"
            assert kwargs["token_url"] == "https://t.example.com/token"
            assert kwargs["scopes"] == ("s1",)

    def test_uses_resolved_token_url_from_base_url(self) -> None:
        manager = _make_manager(token_url="", base_url="https://atlan.example.com")
        with (
            mock.patch(_OAUTH_SERVICE_TARGET),
            mock.patch(_OAUTH_CRED_TARGET) as cred_cls,
        ):
            manager._get_token_service()
            kwargs = cred_cls.call_args.kwargs
            assert kwargs["token_url"].endswith(
                "/auth/realms/default/protocol/openid-connect/token"
            )


# ---------------------------------------------------------------------------
# acquire_initial_token
# ---------------------------------------------------------------------------


class TestAcquireInitialToken:
    @pytest.mark.asyncio
    async def test_returns_token_on_success(self) -> None:
        manager = _make_manager()
        expires = datetime(2026, 1, 1, tzinfo=UTC)
        svc = _stub_token_service(manager, token="initial-token", expires_at=expires)

        token = await manager.acquire_initial_token()

        assert token == "initial-token"
        svc.get_token.assert_awaited_once_with(force_refresh=True)

    @pytest.mark.asyncio
    async def test_wraps_failure_in_runtime_error(self) -> None:
        manager = _make_manager()
        svc = _stub_token_service(manager)
        svc.get_token = mock.AsyncMock(side_effect=ValueError("bad creds"))

        with pytest.raises(TemporalAuthTokenAcquireError):
            await manager.acquire_initial_token()

    @pytest.mark.asyncio
    async def test_handles_unknown_expires_at(self) -> None:
        """When current_expires_at is None the call still succeeds."""
        manager = _make_manager()
        _stub_token_service(manager, token="t", expires_at=None)

        token = await manager.acquire_initial_token()

        assert token == "t"

    @pytest.mark.asyncio
    async def test_runtime_error_chains_original_exception(self) -> None:
        manager = _make_manager()
        original = ValueError("network blip")
        svc = _stub_token_service(manager)
        svc.get_token = mock.AsyncMock(side_effect=original)

        with pytest.raises(TemporalAuthTokenAcquireError) as ei:
            await manager.acquire_initial_token()

        assert ei.value.__cause__ is original


# ---------------------------------------------------------------------------
# start_background_refresh / shutdown
# ---------------------------------------------------------------------------


class TestStartAndShutdown:
    @pytest.mark.asyncio
    async def test_start_creates_named_task(self) -> None:
        manager = _make_manager()
        client = mock.MagicMock(name="Client")

        # Replace _refresh_loop with a no-op coroutine that resolves
        # immediately so the task does not run forever.
        async def _noop_loop(_client) -> None:
            return None

        with mock.patch.object(manager, "_refresh_loop", side_effect=_noop_loop):
            manager.start_background_refresh(client)
            assert manager._refresh_task is not None
            assert manager._refresh_task.get_name() == "temporal-auth-refresh"
            await manager._refresh_task  # let it finish

    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(self) -> None:
        manager = _make_manager()
        client = mock.MagicMock(name="Client")

        async def _noop_loop(_client) -> None:
            # block until cancelled so the task is "still running"
            await asyncio.Event().wait()

        with mock.patch.object(manager, "_refresh_loop", side_effect=_noop_loop):
            manager.start_background_refresh(client)
            first_task = manager._refresh_task

            manager.start_background_refresh(client)  # should warn + return
            assert manager._refresh_task is first_task

            # cleanup
            first_task.cancel()
            try:
                await first_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_shutdown_with_no_task_is_safe(self) -> None:
        """Shutdown before start should not raise."""
        manager = _make_manager()
        await manager.shutdown()
        assert manager._refresh_task is None
        assert manager._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_shutdown_awaits_clean_task(self) -> None:
        manager = _make_manager()

        async def _noop_loop(_client) -> None:
            # exit cleanly when shutdown event is set
            await manager._shutdown_event.wait()

        client = mock.MagicMock(name="Client")
        with mock.patch.object(manager, "_refresh_loop", side_effect=_noop_loop):
            manager.start_background_refresh(client)
            await manager.shutdown()

        assert manager._refresh_task is None
        assert manager._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_hung_task(self) -> None:
        """If the refresh task does not stop within timeout, it is cancelled."""
        manager = _make_manager()

        async def _hang_loop(_client) -> None:
            # Ignore the shutdown event — only cancellation will stop us.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        client = mock.MagicMock(name="Client")

        # Patch wait_for so the timeout path is taken without a real wait.
        async def _instant_timeout(*_args, **_kwargs):
            raise TimeoutError

        with (
            mock.patch.object(manager, "_refresh_loop", side_effect=_hang_loop),
            mock.patch(
                "application_sdk.execution._temporal.auth.asyncio.wait_for",
                side_effect=_instant_timeout,
            ),
        ):
            manager.start_background_refresh(client)
            await manager.shutdown()

        assert manager._refresh_task is None


# ---------------------------------------------------------------------------
# _calculate_sleep_seconds
# ---------------------------------------------------------------------------

# All tests freeze time via time.time; current_expires_at is provided as a
# datetime whose .timestamp() is the reference point.  OAuthTokenService now
# stores the server-issued JWT exp directly so current_expires_at is immune to
# container clock drift; clock_offset_seconds from the service corrects any
# residual gap between local time.time() and the server's wall clock.


class TestCalculateSleepSeconds:
    @pytest.fixture
    def frozen_ts(self) -> float:
        return 1_700_000_000.0

    def test_returns_max_when_expires_at_is_none(self) -> None:
        manager = _make_manager()
        _stub_token_service(manager, expires_at=None)
        assert manager._calculate_sleep_seconds() == 300.0

    def test_returns_min_when_expired(self, frozen_ts: float) -> None:
        manager = _make_manager()
        _stub_token_service(
            manager, expires_at=datetime.fromtimestamp(frozen_ts - 300, UTC)
        )
        with mock.patch("application_sdk.execution._temporal.auth.time") as mock_time:
            mock_time.time.return_value = frozen_ts
            assert manager._calculate_sleep_seconds() == 30.0

    def test_halves_remaining_within_bounds(self, frozen_ts: float) -> None:
        manager = _make_manager()
        _stub_token_service(
            manager, expires_at=datetime.fromtimestamp(frozen_ts + 200, UTC)
        )
        with mock.patch("application_sdk.execution._temporal.auth.time") as mock_time:
            mock_time.time.return_value = frozen_ts
            assert manager._calculate_sleep_seconds() == 100.0

    def test_clamped_to_min(self, frozen_ts: float) -> None:
        manager = _make_manager()
        _stub_token_service(
            manager, expires_at=datetime.fromtimestamp(frozen_ts + 20, UTC)
        )
        with mock.patch("application_sdk.execution._temporal.auth.time") as mock_time:
            mock_time.time.return_value = frozen_ts
            assert manager._calculate_sleep_seconds() == 30.0

    def test_clamped_to_max(self, frozen_ts: float) -> None:
        manager = _make_manager()
        _stub_token_service(
            manager, expires_at=datetime.fromtimestamp(frozen_ts + 3600, UTC)
        )
        with mock.patch("application_sdk.execution._temporal.auth.time") as mock_time:
            mock_time.time.return_value = frozen_ts
            assert manager._calculate_sleep_seconds() == 300.0

    def test_clock_offset_applied(self, frozen_ts: float) -> None:
        manager = _make_manager()
        # Service reports +30s offset (container 30s slow):
        # server_now = frozen_ts + 30 → remaining = 230 - 30 = 200 → sleep = 100
        _stub_token_service(
            manager,
            expires_at=datetime.fromtimestamp(frozen_ts + 230, UTC),
            clock_offset_seconds=30.0,
        )
        with mock.patch("application_sdk.execution._temporal.auth.time") as mock_time:
            mock_time.time.return_value = frozen_ts
            assert manager._calculate_sleep_seconds() == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# _do_refresh
# ---------------------------------------------------------------------------


class TestDoRefresh:
    @pytest.mark.asyncio
    async def test_updates_client_api_key_and_emits_event(self) -> None:
        manager = _make_manager()
        expires = datetime(2026, 6, 1, tzinfo=UTC)
        svc = _stub_token_service(manager, token="new-token", expires_at=expires)
        client = mock.MagicMock(name="Client")
        client.api_key = "old-token"

        with mock.patch.object(
            manager, "_emit_token_refresh_event", new=mock.AsyncMock()
        ) as mock_emit:
            await manager._do_refresh(client)

        assert client.api_key == "new-token"
        svc.get_token.assert_awaited_once_with(force_refresh=True)
        mock_emit.assert_awaited_once_with(expires)

    @pytest.mark.asyncio
    async def test_propagates_get_token_failure(self) -> None:
        """_do_refresh does not swallow exceptions — the loop wraps it."""
        manager = _make_manager()
        svc = _stub_token_service(manager)
        svc.get_token = mock.AsyncMock(side_effect=RuntimeError("boom"))
        client = mock.MagicMock(name="Client")

        with pytest.raises(RuntimeError, match="boom"):
            await manager._do_refresh(client)


# ---------------------------------------------------------------------------
# _refresh_loop
# ---------------------------------------------------------------------------


class TestRefreshLoop:
    @pytest.mark.asyncio
    async def test_exits_immediately_when_shutdown_already_set(self) -> None:
        manager = _make_manager()
        _stub_token_service(manager, expires_at=None)
        client = mock.MagicMock(name="Client")
        manager._shutdown_event.set()

        with mock.patch.object(
            manager, "_do_refresh", new=mock.AsyncMock()
        ) as mock_refresh:
            await manager._refresh_loop(client)

        mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_performs_refresh_when_sleep_times_out(self) -> None:
        """Normal path: sleep → timeout → _do_refresh → next iteration sees shutdown."""
        manager = _make_manager()
        _stub_token_service(manager, expires_at=None)
        client = mock.MagicMock(name="Client")

        # Stop the loop after one refresh by setting the shutdown event.
        async def _refresh_then_stop(_c) -> None:
            manager._shutdown_event.set()

        with (
            mock.patch.object(manager, "_calculate_sleep_seconds", return_value=0.001),
            mock.patch.object(
                manager, "_do_refresh", side_effect=_refresh_then_stop
            ) as mock_refresh,
        ):
            await asyncio.wait_for(manager._refresh_loop(client), timeout=2.0)

        mock_refresh.assert_awaited()

    @pytest.mark.asyncio
    async def test_refresh_failure_triggers_retry_sleep_then_exit(self) -> None:
        """When _do_refresh raises, the loop logs and waits for retry interval.

        We set shutdown so the retry sleep observes it and the loop exits
        cleanly via the `break`.
        """
        manager = _make_manager()
        _stub_token_service(manager, expires_at=None)
        client = mock.MagicMock(name="Client")

        async def _fail(_c) -> None:
            # Set shutdown so the *retry* sleep returns immediately.
            manager._shutdown_event.set()
            raise RuntimeError("token endpoint 500")

        with (
            mock.patch.object(manager, "_calculate_sleep_seconds", return_value=0.001),
            mock.patch.object(manager, "_do_refresh", side_effect=_fail),
        ):
            await asyncio.wait_for(manager._refresh_loop(client), timeout=2.0)

        # Loop must have exited — task is bounded and event is set.
        assert manager._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_refresh_failure_continues_loop_when_not_shutdown(self) -> None:
        """If retry sleep times out (no shutdown), loop continues to retry refresh."""
        manager = _make_manager()
        _stub_token_service(manager, expires_at=None)
        client = mock.MagicMock(name="Client")

        call_count = {"n": 0}

        async def _do_refresh(_c) -> None:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                manager._shutdown_event.set()
                return
            raise RuntimeError("transient")

        with (
            mock.patch.object(manager, "_calculate_sleep_seconds", return_value=0.001),
            mock.patch.object(manager, "_do_refresh", side_effect=_do_refresh),
            # Shorten the retry sleep so the test doesn't wait 30s.
            mock.patch(
                "application_sdk.execution._temporal.auth._RETRY_INTERVAL_SECONDS",
                0.001,
            ),
        ):
            await asyncio.wait_for(manager._refresh_loop(client), timeout=2.0)

        assert call_count["n"] >= 2
