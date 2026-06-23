"""Tests for application_sdk.server.health.

Covers HealthStatus data class (hermetic, no network).

Tests that start a real local TCP server have been moved to
tests/integration/server/test_health.py.
"""

from application_sdk.server.health import HealthStatus

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
