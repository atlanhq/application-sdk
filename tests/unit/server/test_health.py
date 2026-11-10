"""Tests for application_sdk.server.health.

Covers HealthStatus data class and pure state-mutation helpers (hermetic, no network).

Tests that start a real local TCP server have been moved to
tests/integration/server/test_health.py.
"""

import math
from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import patch

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
        stale = _utc_now() - timedelta(seconds=120)
        server._last_activity = stale
        status = await server.check_live()
        assert status.healthy is False
        assert status.details["idle_seconds"] > 30
        # Pin the full operator-visible probe output, not just idle_seconds.
        assert status.details["last_activity"] == stale.isoformat()
        assert status.details["max_idle_seconds"] == 30
        assert status.message == "No worker activity within liveness window"
        # The 503 is the probe an operator reaches for first, so it must carry
        # the same poll-loop evidence the healthy branch serves.
        for key in (
            "poller_counts",
            "poller_counts_read_at",
            "last_fatal_at",
            "last_fatal_type",
            "fatal_count",
        ):
            assert key in status.details

    @pytest.mark.asyncio
    async def test_idle_window_healthy_at_exact_boundary(self):
        """Equality is healthy: production compares with a strict ``>``, so
        idle_seconds == max_idle_seconds must not fail the probe."""
        server = WorkerHealthServer(host="127.0.0.1", port=0, max_idle_seconds=30)
        now = _utc_now()
        server._last_activity = now - timedelta(seconds=30)
        # Freeze "now" so idle_seconds is exactly 30, not 30 + test elapsed time.
        with patch("application_sdk.server.health._utc_now", return_value=now):
            status = await server.check_live()
        assert status.healthy is True

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

    @pytest.mark.parametrize("bad_window", [-10, math.inf, math.nan])
    @pytest.mark.asyncio
    async def test_non_positive_or_non_finite_window_disables_check(
        self, bad_window: float
    ):
        """The constructor normalizes negative / inf / nan windows to disabled
        (mirrors the env loader). A stale last_activity must still be healthy so
        the ``> 0`` / ``math.isfinite`` guard can't silently regress."""
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, max_idle_seconds=bad_window
        )
        assert server._max_idle_seconds is None
        server._last_activity = _utc_now() - timedelta(hours=1)
        assert (await server.check_live()).healthy is True


@dataclass(frozen=True)
class _StubTemporalClient:
    """Satisfies ``TemporalClientProtocol`` for readiness checks."""

    identity: str


class TestPollDiagnostics:
    """Poll-loop diagnostics surface on the probes (ARUN-1127).

    With the poll-loop checks left at their constructor defaults (disabled),
    none of these fields flips a probe. The checks are covered in
    ``TestLivePollLoopChecks``.
    """

    @pytest.mark.asyncio
    async def test_fatal_and_poller_counts_surface_in_live_details(self):
        server = WorkerHealthServer(host="127.0.0.1", port=0)
        server.record_worker_fatal(RuntimeError("Activity worker failed"))
        server.record_poller_counts({"workflow_task": 0.0})

        status = await server.check_live()

        assert status.details["last_fatal_type"] == "RuntimeError"
        assert status.details["fatal_count"] == 1
        assert status.details["poller_counts"] == {"workflow_task": 0.0}
        assert status.details["poller_counts_read_at"] is not None

    @pytest.mark.asyncio
    async def test_fatal_message_is_not_exposed_over_http(self):
        """Only the exception type is retained; the chain goes to the logs.

        The probe payload is served over HTTP and a gRPC status repr can carry
        response metadata, so the message must not land in it.
        """
        server = WorkerHealthServer(host="127.0.0.1", port=0)
        server.record_worker_fatal(RuntimeError("Poll failure: token abcdef123"))

        details = (await server.check_live()).details

        assert "abcdef123" not in str(details)

    @pytest.mark.asyncio
    async def test_zero_pollers_do_not_flip_live_when_checks_disabled(self):
        server = WorkerHealthServer(host="127.0.0.1", port=0)
        server.record_poller_counts({"workflow_task": 0.0, "activity_task": 0.0})
        server.record_worker_fatal(RuntimeError("Workflow worker failed"))

        assert (await server.check_live()).healthy is True

    @pytest.mark.asyncio
    async def test_unknown_reading_stays_none_not_zero(self):
        """An unreadable gauge is recorded as unknown, never as a zero count."""
        server = WorkerHealthServer(host="127.0.0.1", port=0)
        server.record_poller_counts(None)

        assert (await server.check_live()).details["poller_counts"] is None

    @pytest.mark.asyncio
    async def test_ready_details_carry_the_same_diagnostics(self):
        server = WorkerHealthServer(host="127.0.0.1", port=0)
        server.set_temporal_client(_StubTemporalClient(identity="1@host"))
        server.record_poller_counts({"activity_task": 4.0})

        details = (await server.check_ready()).details

        assert details["identity"] == "1@host"
        assert details["poller_counts"] == {"activity_task": 4.0}


class TestLivePollLoopChecks:
    """``/live`` fails on a confirmed-dead poll loop (FND-4641)."""

    _ZERO = {"workflow_task": 0.0, "activity_task": 0.0}
    _POLLING = {"workflow_task": 2.0, "activity_task": 0.0}

    @pytest.mark.asyncio
    async def test_zero_readings_trip_live_at_threshold(self):
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, max_zero_poller_readings=3
        )
        server.record_poller_counts(self._ZERO)
        server.record_poller_counts(self._ZERO)
        assert (await server.check_live()).healthy is True

        server.record_poller_counts(self._ZERO)
        status = await server.check_live()

        assert status.healthy is False
        assert status.message == "Worker reports zero active pollers"
        assert status.details["consecutive_zero_poller_readings"] == 3
        assert status.details["poller_counts"] == self._ZERO

    @pytest.mark.asyncio
    @pytest.mark.parametrize("interrupting", [None, {"activity_task": 1.0}])
    async def test_non_zero_reading_resets_the_streak(
        self, interrupting: dict[str, float] | None
    ):
        """``unknown`` and ``polling`` both fail open and restart the count."""
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, max_zero_poller_readings=2
        )
        server.record_poller_counts(self._ZERO)
        server.record_poller_counts(interrupting)
        server.record_poller_counts(self._ZERO)

        assert (await server.check_live()).healthy is True

    @pytest.mark.asyncio
    async def test_recovery_after_tripping_returns_live_to_healthy(self):
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, max_zero_poller_readings=1
        )
        server.record_poller_counts(self._ZERO)
        assert (await server.check_live()).healthy is False

        server.record_poller_counts(self._POLLING)
        assert (await server.check_live()).healthy is True

    @pytest.mark.asyncio
    async def test_empty_gauge_counts_as_zero(self):
        """A readable gauge with no poller series is a definitive zero."""
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, max_zero_poller_readings=1
        )
        server.record_poller_counts({})

        assert (await server.check_live()).healthy is False

    @pytest.mark.asyncio
    async def test_unrecovered_fatal_trips_live_after_bound(self):
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, fatal_recovery_seconds=600
        )
        server.record_worker_fatal(RuntimeError("Activity worker failed"))
        assert (await server.check_live()).healthy is True

        later = _utc_now() + timedelta(seconds=601)
        with patch("application_sdk.server.health._utc_now", return_value=later):
            status = await server.check_live()

        assert status.healthy is False
        assert status.message == "Worker poll loop failed fatally and has not recovered"
        assert status.details["fatal_unrecovered_since"] is not None
        assert status.details["fatal_recovery_seconds"] == 600

    @pytest.mark.asyncio
    async def test_polling_reading_after_fatal_counts_as_recovery(self):
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, fatal_recovery_seconds=600
        )
        server.record_worker_fatal(RuntimeError("Activity worker failed"))
        server.record_poller_counts(self._POLLING)

        later = _utc_now() + timedelta(seconds=601)
        with patch("application_sdk.server.health._utc_now", return_value=later):
            status = await server.check_live()

        assert status.healthy is True
        assert status.details["fatal_unrecovered_since"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reading", [None, {"activity_task": 0.0}])
    async def test_unknown_or_zero_reading_is_not_recovery(
        self, reading: dict[str, float] | None
    ):
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, fatal_recovery_seconds=600
        )
        server.record_worker_fatal(RuntimeError("Activity worker failed"))
        server.record_poller_counts(reading)

        later = _utc_now() + timedelta(seconds=601)
        with patch("application_sdk.server.health._utc_now", return_value=later):
            assert (await server.check_live()).healthy is False

    @pytest.mark.asyncio
    async def test_activity_heartbeat_is_not_recovery(self):
        """In-flight activities keep heartbeating while a failed worker drains."""
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, fatal_recovery_seconds=600
        )
        server.record_worker_fatal(RuntimeError("Activity worker failed"))
        server.record_activity()

        later = _utc_now() + timedelta(seconds=601)
        with patch("application_sdk.server.health._utc_now", return_value=later):
            assert (await server.check_live()).healthy is False

    @pytest.mark.asyncio
    async def test_repeat_fatal_keeps_the_first_deadline(self):
        """A crash loop that never recovers must not keep pushing the bound out."""
        start = _utc_now()
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, fatal_recovery_seconds=600
        )
        with patch("application_sdk.server.health._utc_now", return_value=start):
            server.record_worker_fatal(RuntimeError("first"))
        with patch(
            "application_sdk.server.health._utc_now",
            return_value=start + timedelta(seconds=500),
        ):
            server.record_worker_fatal(RuntimeError("second"))
        with patch(
            "application_sdk.server.health._utc_now",
            return_value=start + timedelta(seconds=601),
        ):
            assert (await server.check_live()).healthy is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, -1, math.inf, math.nan])
    async def test_non_positive_or_non_finite_fatal_bound_disables_check(
        self, bad: float
    ):
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, fatal_recovery_seconds=bad
        )
        server.record_worker_fatal(RuntimeError("Activity worker failed"))

        later = _utc_now() + timedelta(days=1)
        with patch("application_sdk.server.health._utc_now", return_value=later):
            assert (await server.check_live()).healthy is True


class TestBuildWorkerHealthServer:
    """The boot helper wires the FND-4641 checks from constants."""

    def test_wires_poll_loop_checks_from_constants(self):
        from application_sdk.server.health import build_worker_health_server

        with (
            patch(
                "application_sdk.constants.WORKER_LIVENESS_MAX_ZERO_POLLER_READINGS", 7
            ),
            patch(
                "application_sdk.constants.WORKER_LIVENESS_FATAL_RECOVERY_SECONDS",
                120.0,
            ),
        ):
            server = build_worker_health_server(
                port=0, client=_StubTemporalClient(identity="1@host")
            )

        assert server._max_zero_poller_readings == 7
        assert server._fatal_recovery_seconds == 120.0
