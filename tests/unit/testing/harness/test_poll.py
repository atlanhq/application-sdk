"""Unit tests for the shared bounded-poll helper.

The defect this helper exists to remove: every hand-rolled loop it replaced
checked its budget *after* the work and *before* the sleep, so it slept one full
interval past its stated timeout. The ``is_last`` tests below pin the fix — a
loop must never wait beyond ``timeout_seconds``, and the call site must always
get exactly one attempt flagged as its last chance to raise.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from application_sdk.testing.harness import _poll
from application_sdk.testing.harness._poll import (
    Attempt,
    FakeClock,
    fake_clock,
    until_deadline,
    until_deadline_async,
)


def _drain(
    timeout: float, interval: float, **kwargs
) -> tuple[list[Attempt], FakeClock]:
    """Run a sync loop to exhaustion under a fake clock, returning what it yielded."""
    clock = FakeClock()
    attempts = list(
        until_deadline(
            timeout,
            interval,
            label="thing",
            clock=clock.monotonic,
            sleep=clock.sleep,
            **kwargs,
        )
    )
    return attempts, clock


async def _drain_async(
    timeout: float, interval: float, **kwargs
) -> tuple[list[Attempt], FakeClock]:
    """Async twin of :func:`_drain`."""
    clock = FakeClock()
    attempts = [
        attempt
        async for attempt in until_deadline_async(
            timeout,
            interval,
            label="thing",
            clock=clock.monotonic,
            sleep=clock.async_sleep,
            **kwargs,
        )
    ]
    return attempts, clock


class TestDeadlineIsRespected:
    """The loop must not sleep, or poll, past its own budget."""

    def test_never_sleeps_past_the_deadline(self):
        """The regression this helper exists for: total slept <= the budget.

        The hand-rolled loops each checked the deadline after the work and before
        the sleep, so a 10s budget at a 3s interval waited 12s.
        """
        attempts, clock = _drain(10, 3)

        assert clock.now <= 10
        assert sum(clock.slept) <= 10
        assert [a.elapsed for a in attempts] == [0, 3, 6, 9]

    def test_final_attempt_is_the_last_one_that_fits(self):
        """Exactly one attempt carries is_last, and it is the final one."""
        attempts, _ = _drain(10, 3)

        assert [a.is_last for a in attempts] == [False, False, False, True]

    def test_one_attempt_when_the_budget_is_a_single_interval(self):
        """A budget with no room for a second poll gets one attempt, flagged last."""
        attempts, clock = _drain(10, 10)

        assert len(attempts) == 1
        assert attempts[0].is_last is True
        assert clock.slept == []

    @pytest.mark.parametrize("timeout", [0, -5])
    def test_zero_or_negative_budget_still_probes_once(self, timeout: float):
        """A probe loop always looks at least once, rather than failing unchecked.

        The call site's exhaustion branch is keyed on ``is_last``, so a loop that
        yielded nothing would raise nothing — the failure would vanish.
        """
        attempts, clock = _drain(timeout, 5)

        assert len(attempts) == 1
        assert attempts[0].is_last is True
        assert attempts[0].remaining == 0
        assert clock.slept == []

    def test_a_slow_probe_does_not_push_the_loop_past_the_deadline(self):
        """A probe that outlasts its interval must shrink the next gap, not add to it.

        timeout=10, interval=3, one 8s probe: sleeping a full 3s on top of it would
        end the loop at 11s. The gap is re-clamped against the clock read after the
        probe returns.
        """
        clock = FakeClock()
        seen: list[Attempt] = []
        for attempt in until_deadline(
            10, 3, label="thing", clock=clock.monotonic, sleep=clock.sleep
        ):
            seen.append(attempt)
            if attempt.number == 1:
                clock.now += 8  # the probe itself takes 8s

        assert clock.slept == [2]
        assert clock.now == 10
        assert [a.is_last for a in seen] == [False, True]

    def test_a_probe_that_blows_the_budget_still_gets_a_final_attempt(self):
        """Clamping the gap to zero must not cost the call site its raise.

        The exhaustion branch keys on is_last, so a loop that returned silently
        here would swallow the failure.
        """
        clock = FakeClock()
        seen: list[Attempt] = []
        for attempt in until_deadline(
            10, 3, label="thing", clock=clock.monotonic, sleep=clock.sleep
        ):
            seen.append(attempt)
            if attempt.number == 1:
                clock.now += 50  # the probe alone outlasts the whole budget

        assert clock.slept == [0]
        assert [a.is_last for a in seen] == [False, True]

    def test_zero_interval_spins_to_the_deadline(self):
        """interval=0 is legal (the worker-health tier uses it) and terminates.

        Deliberately on the real clock: a spin advances only because wall time
        does, so :class:`FakeClock` — which advances only on sleep — cannot model
        it and would loop forever.
        """
        attempts = list(until_deadline(0.05, 0, label="thing", heartbeat_seconds=0))

        assert len(attempts) >= 1
        assert attempts[-1].is_last is True
        assert attempts[-1].elapsed >= 0.05


class TestAttemptFields:
    """The typed fields exist so an error leaf never re-derives them by hand."""

    def test_number_elapsed_and_remaining(self):
        attempts, _ = _drain(10, 3)

        assert [a.number for a in attempts] == [1, 2, 3, 4]
        assert [a.remaining for a in attempts] == [10, 7, 4, 1]

    def test_remaining_never_goes_negative(self):
        """Budgets that don't divide by the interval must not report a debt."""
        attempts, _ = _drain(10, 4)

        assert all(a.remaining >= 0 for a in attempts)
        assert attempts[-1].remaining == 2


class TestEarlyExit:
    """Breaking out of the loop must stop it dead, not sleep once more."""

    def test_break_on_first_attempt_sleeps_nothing(self):
        clock = FakeClock()
        for _attempt in until_deadline(
            100, 5, label="thing", clock=clock.monotonic, sleep=clock.sleep
        ):
            break

        assert clock.slept == []
        assert clock.now == 0

    def test_break_partway_stops_the_clock(self):
        clock = FakeClock()
        for attempt in until_deadline(
            100, 5, label="thing", clock=clock.monotonic, sleep=clock.sleep
        ):
            if attempt.number == 3:
                break

        assert clock.slept == [5, 5]


class TestSleepNext:
    """Call sites that honour an origin-supplied backoff replace one gap only."""

    def test_honours_a_longer_requested_gap(self):
        clock = FakeClock()
        for attempt in until_deadline(
            1000, 10, label="thing", clock=clock.monotonic, sleep=clock.sleep
        ):
            if attempt.number == 1:
                attempt.sleep_next(120)
            if attempt.number == 3:
                break

        # First gap honoured at 120s, then straight back to the poll cadence —
        # the request applies to one sleep, not to the rest of the loop.
        assert clock.slept == [120, 10]

    def test_clamps_the_gap_to_the_remaining_budget(self):
        """A 120s backoff against a 50s residual budget must not overshoot it."""
        clock = FakeClock()
        gaps: list[float] = []
        for attempt in until_deadline(
            50, 10, label="thing", clock=clock.monotonic, sleep=clock.sleep
        ):
            gaps.append(attempt.sleep_next(120))

        assert gaps == [50, 0]  # second call lands on the last attempt: no-op
        assert clock.slept == [50]
        assert clock.now == 50

    def test_returns_the_gap_actually_taken(self):
        """The return value is what the call site should log, not the request."""
        clock = FakeClock()
        loop = until_deadline(
            50, 10, label="thing", clock=clock.monotonic, sleep=clock.sleep
        )
        assert next(loop).sleep_next(30) == 30

    def test_negative_request_floors_at_zero(self):
        clock = FakeClock()
        loop = until_deadline(
            50, 10, label="thing", clock=clock.monotonic, sleep=clock.sleep
        )
        assert next(loop).sleep_next(-5) == 0


class TestHeartbeat:
    """One shared heartbeat instead of five loops that go silent for minutes."""

    def _heartbeats(self, timeout: float, interval: float, **kwargs) -> list[str]:
        """Drain a loop and return the rendered heartbeat lines it emitted."""
        with patch.object(_poll, "logger") as mock_logger:
            _drain(timeout, interval, **kwargs)
        return [
            call.args[0] % call.args[1:] for call in mock_logger.info.call_args_list
        ]

    def test_fires_at_the_configured_cadence(self):
        # Attempts land at 0,10,...,90; the heartbeat fires at 30, 60 and 90.
        assert len(self._heartbeats(100, 10, heartbeat_seconds=30)) == 3

    def test_zero_disables_it(self):
        assert self._heartbeats(100, 10, heartbeat_seconds=0) == []

    def test_names_the_label_and_the_budget(self):
        with patch.object(_poll, "logger") as mock_logger:
            clock = FakeClock()
            list(
                until_deadline(
                    100,
                    40,
                    label="worker health at http://localhost:8000/server/health",
                    heartbeat_seconds=30,
                    clock=clock.monotonic,
                    sleep=clock.sleep,
                )
            )

        call = mock_logger.info.call_args_list[0]
        message = call.args[0] % call.args[1:]
        assert "worker health at http://localhost:8000/server/health" in message
        assert "attempt 2" in message
        assert "40s of 100s elapsed" in message
        assert "60s left" in message


class TestAsyncTwin:
    """The async generator must not drift from the sync one."""

    @pytest.mark.parametrize(
        ("timeout", "interval"),
        [(10, 3), (10, 10), (0, 5), (50, 4), (7, 2)],
    )
    async def test_yields_the_same_attempts_as_the_sync_loop(
        self, timeout: float, interval: float
    ):
        sync_attempts, sync_clock = _drain(timeout, interval)
        async_attempts, async_clock = await _drain_async(timeout, interval)

        assert async_attempts == sync_attempts
        assert async_clock.slept == sync_clock.slept

    async def test_a_slow_probe_does_not_push_the_loop_past_the_deadline(self):
        """The post-probe gap clamp must hold in the async twin too."""
        clock = FakeClock()
        seen: list[Attempt] = []
        async for attempt in until_deadline_async(
            10, 3, label="thing", clock=clock.monotonic, sleep=clock.async_sleep
        ):
            seen.append(attempt)
            if attempt.number == 1:
                clock.now += 8

        assert clock.slept == [2]
        assert clock.now == 10
        assert [a.is_last for a in seen] == [False, True]

    async def test_a_probe_that_blows_the_budget_still_gets_a_final_attempt(self):
        """Clamping the async gap to zero must not cost the call site its raise."""
        clock = FakeClock()
        seen: list[Attempt] = []
        async for attempt in until_deadline_async(
            10, 3, label="thing", clock=clock.monotonic, sleep=clock.async_sleep
        ):
            seen.append(attempt)
            if attempt.number == 1:
                clock.now += 50  # the probe alone outlasts the whole budget

        assert clock.slept == [0]
        assert [a.is_last for a in seen] == [False, True]

    async def test_honours_sleep_next(self):
        clock = FakeClock()
        async for attempt in until_deadline_async(
            1000,
            10,
            label="thing",
            clock=clock.monotonic,
            sleep=clock.async_sleep,
        ):
            if attempt.number == 1:
                attempt.sleep_next(120)
            if attempt.number == 3:
                break

        assert clock.slept == [120, 10]

    async def test_awaits_the_real_asyncio_sleep_by_default(self):
        """No clock injected → real timers, and the budget still bounds the loop."""
        started = time.monotonic()
        attempts = [
            attempt
            async for attempt in until_deadline_async(
                0.06, 0.02, label="thing", heartbeat_seconds=0
            )
        ]

        assert len(attempts) >= 2
        assert attempts[-1].is_last is True
        # Bounded by the budget (plus scheduler slop), not by budget + one interval.
        assert time.monotonic() - started < 0.5


class TestFakeClock:
    """The test seam itself: it must not disturb the process-wide clock."""

    def test_leaves_time_monotonic_alone(self):
        """A global clock patch breaks the asyncio loop's own timers."""
        real_monotonic = time.monotonic
        with fake_clock():
            assert time.monotonic is real_monotonic
            assert time.sleep is not None
            before = time.monotonic()
            assert time.monotonic() >= before

    def test_swaps_the_poll_defaults_and_restores_them(self):
        saved = (_poll._monotonic, _poll._sleep, _poll._async_sleep)
        with fake_clock() as clock:
            # `==`, not `is`: a bound method is a fresh object per attribute read.
            assert _poll._monotonic == clock.monotonic
            assert _poll._sleep == clock.sleep
            assert _poll._async_sleep == clock.async_sleep
        assert (_poll._monotonic, _poll._sleep, _poll._async_sleep) == saved

    def test_restores_the_defaults_after_an_exception(self):
        saved = (_poll._monotonic, _poll._sleep, _poll._async_sleep)
        with pytest.raises(RuntimeError, match="boom"), fake_clock():
            raise RuntimeError("boom")
        assert (_poll._monotonic, _poll._sleep, _poll._async_sleep) == saved

    def test_drives_a_loop_that_takes_no_clock_arguments(self):
        """The point of the seam: poll code with no injectable clock still runs fast."""
        with fake_clock() as clock:
            attempts = list(until_deadline(600, 30, label="thing"))

        assert len(attempts) == 20
        assert clock.now == 570

    async def test_drives_an_async_loop_without_touching_the_event_loop_clock(self):
        started = time.monotonic()
        with fake_clock() as clock:
            attempts = [
                attempt
                async for attempt in until_deadline_async(600, 30, label="thing")
            ]

        assert len(attempts) == 20
        assert clock.now == 570
        # A 570s simulated wait that took no real time at all.
        assert time.monotonic() - started < 1.0
