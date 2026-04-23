"""Tests for _emit_token_refresh_event in TemporalAuthManager."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest import mock

import pytest

from application_sdk.contracts.events import ApplicationEventNames, EventTypes
from application_sdk.execution._temporal.auth import (
    TemporalAuthConfig,
    TemporalAuthManager,
)

# The method imports _publish_event_via_binding locally, so we mock at its
# source module rather than on the auth module.
_PUBLISH_TARGET = (
    "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding"
)


def _make_manager() -> TemporalAuthManager:
    """Create a TemporalAuthManager with dummy config for testing."""
    return TemporalAuthManager(
        config=TemporalAuthConfig(
            client_id="test-client",
            client_secret="test-secret",
            token_url="https://example.com/token",
        )
    )


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

        env_vars = {
            "ATLAN_APP_NAME": "my-test-app",
            "ATLAN_DEPLOYMENT_NAME": "my-deployment",
        }

        with (
            mock.patch.dict("os.environ", env_vars, clear=False),
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
        """When ATLAN_DEPLOYMENT_NAME is unset, deployment_name equals app_name."""
        manager = _make_manager()
        expires_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        # Set only ATLAN_APP_NAME, ensure ATLAN_DEPLOYMENT_NAME is absent
        env_overrides = {"ATLAN_APP_NAME": "fallback-app"}

        with (
            mock.patch.dict("os.environ", env_overrides, clear=False),
            mock.patch(_PUBLISH_TARGET) as mock_publish,
        ):
            # Remove ATLAN_DEPLOYMENT_NAME if it happens to be set
            import os

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
