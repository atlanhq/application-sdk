"""Tests for the poll-state gate on worker health events.

The gate decides whether a worker keeps advertising itself as a healthy agent.
A false positive marks a *live* worker dead across the fleet, so the tests below
are weighted toward the conservative direction: everything that is not a
sustained, definitive zero must keep publishing.
"""

from __future__ import annotations

from application_sdk.execution._temporal.poll_state import WorkerPollState


class TestWorkerPollState:
    def test_publishes_by_default(self) -> None:
        """An unwired gate — no observer running — publishes as before."""
        assert WorkerPollState().should_emit_health_event() is True

    def test_zero_below_threshold_still_publishes(self) -> None:
        """A blip must not mute the worker; only a sustained zero counts."""
        state = WorkerPollState()
        state.configure(zero_readings_before_stale=3)

        for _ in range(2):
            state.record("zero")
            assert state.should_emit_health_event() is True

    def test_sustained_zero_suppresses(self) -> None:
        """At the threshold the worker stops advertising itself as healthy."""
        state = WorkerPollState()
        state.configure(zero_readings_before_stale=3)

        for _ in range(3):
            state.record("zero")

        assert state.should_emit_health_event() is False

    def test_unknown_never_suppresses(self) -> None:
        """`unknown` is the normal startup state and a transient read failure.

        Suppressing on it would mark healthy agents dead fleet-wide, so it must
        never accumulate toward the threshold no matter how long it persists.
        """
        state = WorkerPollState()
        state.configure(zero_readings_before_stale=2)

        for _ in range(20):
            state.record("unknown")

        assert state.should_emit_health_event() is True

    def test_unknown_clears_accumulated_zeros(self) -> None:
        """Losing the reading is not evidence of death — it reopens the gate."""
        state = WorkerPollState()
        state.configure(zero_readings_before_stale=2)

        state.record("zero")
        state.record("unknown")
        state.record("zero")

        # Two zeros seen, but not consecutively, so the gate stays open.
        assert state.should_emit_health_event() is True

    def test_polling_resumes_publishing(self) -> None:
        """Recovery is automatic the moment the poll loop comes back."""
        state = WorkerPollState()
        state.configure(zero_readings_before_stale=2)

        state.record("zero")
        state.record("zero")
        assert state.should_emit_health_event() is False

        state.record("polling")
        assert state.should_emit_health_event() is True

    def test_reset_reopens_the_gate(self) -> None:
        """Shutdown must never leave a worker muted for its next life."""
        state = WorkerPollState()
        state.configure(zero_readings_before_stale=1)

        state.record("zero")
        assert state.should_emit_health_event() is False

        state.reset()
        assert state.should_emit_health_event() is True

    def test_threshold_floor_is_one(self) -> None:
        """A nonsensical threshold must not mute on a single stray reading."""
        state = WorkerPollState()
        state.configure(zero_readings_before_stale=0)

        assert state.should_emit_health_event() is True
        state.record("zero")
        assert state.should_emit_health_event() is False
