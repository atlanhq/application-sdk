"""Tests for application_sdk.server.health.

Covers HealthStatus data class and pure state-mutation helpers (hermetic, no network).

Tests that start a real local TCP server have been moved to
tests/integration/server/test_health.py.
"""

import pytest

from application_sdk.server.health import HealthStatus, WorkerHealthServer

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
