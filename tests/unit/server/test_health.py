"""Tests for application_sdk.server.health.

Covers HealthStatus data class and pure state-mutation helpers (hermetic, no network).

Tests that start a real local TCP server have been moved to
tests/integration/server/test_health.py.
"""

from datetime import timedelta

import pytest

from application_sdk.server.health import HealthStatus, WorkerHealthServer, _utc_now

# ---------------------------------------------------------------------------
# HealthStatus
# ---------------------------------------------------------------------------


class TestHealthStatus:
    def test_to_dict_healthy(self):
        status = HealthStatus(healthy=True, message="OK")
        d = status.to_dict()
        assert d["healthy"] is True
        assert d["message"] == "OK"
        assert "checked_at" in d

    def test_to_dict_unhealthy(self):
        status = HealthStatus(healthy=False, message="Not ready")
        d = status.to_dict()
        assert d["healthy"] is False

    def test_to_dict_with_details(self):
        status = HealthStatus(healthy=True, details={"key": "value"})
        d = status.to_dict()
        assert d["details"]["key"] == "value"


# ---------------------------------------------------------------------------
# WorkerHealthServer — pure state helpers (no server started)
# ---------------------------------------------------------------------------


class TestWorkerHealthServerState:
    @pytest.mark.asyncio
    async def test_record_activity_updates_last_activity(self):
        server = WorkerHealthServer(host="127.0.0.1", port=0)
        assert server._last_activity is None
        server.record_activity()
        assert server._last_activity is not None


class TestCheckLive:
    """check_live: optional activity-staleness window (default disabled)."""

    @pytest.mark.asyncio
    async def test_healthy_when_no_window(self):
        """Default posture: no window — always healthy (never false-fails an
        idle queue)."""
        server = WorkerHealthServer(host="127.0.0.1", port=0)
        status = await server.check_live()
        assert status.healthy is True

    @pytest.mark.asyncio
    async def test_idle_window_disabled_by_default(self):
        """Even with a stale last_activity, no configured window means healthy."""
        server = WorkerHealthServer(host="127.0.0.1", port=0)
        server._last_activity = _utc_now() - timedelta(hours=1)
        status = await server.check_live()
        assert status.healthy is True

    @pytest.mark.asyncio
    async def test_idle_window_unhealthy_when_stale(self):
        server = WorkerHealthServer(host="127.0.0.1", port=0, max_idle_seconds=30)
        server._last_activity = _utc_now() - timedelta(seconds=120)
        status = await server.check_live()
        assert status.healthy is False
        assert status.details["idle_seconds"] > 30

    @pytest.mark.asyncio
    async def test_idle_window_healthy_when_recent(self):
        server = WorkerHealthServer(host="127.0.0.1", port=0, max_idle_seconds=300)
        server.record_activity()
        status = await server.check_live()
        assert status.healthy is True

    @pytest.mark.asyncio
    async def test_idle_window_healthy_when_never_active(self):
        """A configured window must not fail before any activity is recorded —
        avoids killing a worker during its startup grace period."""
        server = WorkerHealthServer(host="127.0.0.1", port=0, max_idle_seconds=30)
        status = await server.check_live()
        assert status.healthy is True

    @pytest.mark.asyncio
    async def test_zero_max_idle_seconds_disables_window(self):
        server = WorkerHealthServer(host="127.0.0.1", port=0, max_idle_seconds=0)
        # Disabled window: /live stays healthy even with no activity recorded.
        assert (await server.check_live()).healthy is True
