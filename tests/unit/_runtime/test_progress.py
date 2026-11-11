"""Unit tests for application_sdk._runtime.progress.

No worker, no event loop, no patched global clock: the tracker takes its clock
by injection precisely so tests can drive time without touching
``time.monotonic`` (which an asyncio loop shares — patching it globally makes
the loop itself misbehave).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from application_sdk._runtime.progress import ClosedHold, ProgressTracker

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeClock:
    """A monotonic clock the test advances explicitly."""

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class HoldRecorder:
    """Collects the ClosedHold observations the tracker reports."""

    closed: list[ClosedHold] = field(default_factory=list)

    def __call__(self, hold: ClosedHold) -> None:
        self.closed.append(hold)


def _tracker(clock: FakeClock, recorder: HoldRecorder | None = None) -> ProgressTracker:
    return ProgressTracker(clock=clock, on_hold_closed=recorder)


# ---------------------------------------------------------------------------
# Progress signal
# ---------------------------------------------------------------------------


class TestMarkProgress:
    def test_stall_clock_starts_at_construction(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)

        assert tracker.stalled_for() == 0.0
        clock.advance(42.0)
        assert tracker.stalled_for() == 42.0

    def test_mark_progress_resets_the_stall_clock(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)

        clock.advance(30.0)
        tracker.mark_progress("write_batch")
        assert tracker.stalled_for() == 0.0

        clock.advance(5.0)
        assert tracker.stalled_for() == 5.0

    def test_label_is_remembered(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)

        assert tracker.last_label == ""
        tracker.mark_progress("write_batch")
        assert tracker.last_label == "write_batch"

    def test_unlabelled_progress_re_arms_without_erasing_the_label(self) -> None:
        """Warn mode re-arms after reporting a gap; the reported label must survive."""
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.mark_progress("transfer_chunk")

        clock.advance(900.0)
        tracker.mark_progress()

        assert tracker.stalled_for() == 0.0
        assert tracker.last_label == "transfer_chunk"


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------


class TestUnboundedHold:
    def test_vouches_indefinitely(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.enter_hold("full table scan", None)

        clock.advance(6 * 60 * 60)

        assert tracker.held() is True
        assert tracker.stalled_for() == 0.0

    def test_exit_resumes_the_stall_clock_from_zero(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)
        token = tracker.enter_hold("full table scan", None)

        clock.advance(3600.0)
        tracker.exit_hold(token)

        assert tracker.held() is False
        assert tracker.stalled_for() == 0.0
        assert tracker.last_label == "full table scan"

        clock.advance(10.0)
        assert tracker.stalled_for() == 10.0


class TestBoundedHold:
    def test_vouches_until_the_allowance_is_spent(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.enter_hold("snapshot metadata query", 1800.0)

        clock.advance(1799.0)
        assert tracker.held() is True
        assert tracker.stalled_for() == 0.0

    def test_lapsed_hold_resumes_the_stall_clock_from_its_deadline(self) -> None:
        """Kill time for a wedged held call is allowance + budget, not allowance.

        The allowance vouched for everything up to the deadline, so the stall
        clock counts from the deadline — not from the last progress signal
        before the hold, which would fire the instant the allowance lapsed.
        """
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.mark_progress("write_batch")
        tracker.enter_hold("snapshot metadata query", 1800.0)

        clock.advance(1800.0 + 700.0)

        assert tracker.held() is False
        assert tracker.stalled_for() == 700.0

    def test_progress_inside_a_lapsed_hold_wins_over_the_deadline(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.enter_hold("paged export", 60.0)

        clock.advance(100.0)
        tracker.mark_progress("fetch_page")
        clock.advance(5.0)

        assert tracker.stalled_for() == 5.0

    def test_zero_allowance_does_not_forgive_the_quiet_before_it(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.mark_progress("write_batch")
        clock.advance(10.0)
        tracker.enter_hold("no allowance at all", 0.0)

        assert tracker.held() is False
        assert tracker.stalled_for() == 10.0

    def test_negative_allowance_is_already_exhausted(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.mark_progress("write_batch")
        clock.advance(10.0)
        tracker.enter_hold("typo", -5.0)

        assert tracker.held() is False
        assert tracker.stalled_for() == 10.0


class TestConcurrentHolds:
    def test_tokens_are_distinct(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)

        first = tracker.enter_hold("query a", None)
        second = tracker.enter_hold("query b", None)

        assert first != second

    def test_exiting_one_hold_does_not_release_another(self) -> None:
        """Keyed by token, not popped off a stack: gather() over two offloads."""
        clock = FakeClock()
        tracker = _tracker(clock)
        short = tracker.enter_hold("quick query", 60.0)
        tracker.enter_hold("slow query", 7200.0)

        clock.advance(30.0)
        tracker.exit_hold(short)

        clock.advance(1000.0)
        assert tracker.held() is True
        assert tracker.stalled_for() == 0.0

    def test_stall_waits_for_the_last_deadline_to_lapse(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.enter_hold("quick query", 60.0)
        tracker.enter_hold("slow query", 600.0)

        clock.advance(300.0)
        assert tracker.stalled_for() == 0.0

        clock.advance(400.0)  # now 700s in: both deadlines are behind us
        assert tracker.stalled_for() == 100.0

    def test_reverse_order_exit_is_fine(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)
        first = tracker.enter_hold("query a", None)
        second = tracker.enter_hold("query b", None)

        tracker.exit_hold(first)
        assert tracker.held() is True
        tracker.exit_hold(second)
        assert tracker.held() is False

    def test_token_allocation_is_thread_safe(self) -> None:
        """contextvars reach worker threads, so holds can be entered off-loop.

        Two holds sharing a token would let one release the other's deadline.
        """
        tracker = ProgressTracker(clock=time.monotonic)

        def enter_many() -> list[int]:
            return [tracker.enter_hold("offloaded call", None) for _ in range(200)]

        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = [pool.submit(enter_many) for _ in range(8)]
            tokens = [token for batch in batches for token in batch.result()]

        assert len(tokens) == 8 * 200
        assert len(set(tokens)) == len(tokens)

    def test_tokens_are_unique_across_live_trackers(self) -> None:
        """Tokens come from a process-wide counter, not a per-tracker one.

        Two concurrently bound trackers (a nested attempt, or a test binding its
        own tracker inside an activity) must never both own the same token —
        otherwise a consumer pairing one tracker's ``enter_hold`` with the
        other's ``exit_hold`` would silently release the wrong hold.
        """
        clock = FakeClock()
        first = _tracker(clock)
        second = _tracker(clock)

        first_token = first.enter_hold("query on first", None)
        second_token = second.enter_hold("query on second", None)

        assert first_token != second_token

    def test_cross_tracker_exit_does_not_release_the_hold(self) -> None:
        """A token from one tracker is unknown to another live tracker.

        The cross-tracker ``exit_hold`` must hit the unknown-token path (no hold
        closed, no progress made) and leave the real hold vouching, rather than
        silently releasing it.
        """
        clock = FakeClock()
        recorder = HoldRecorder()
        first = _tracker(clock, recorder)
        second = _tracker(clock, recorder)
        token = first.enter_hold("full table scan", None)
        second.enter_hold("slow export", None)

        clock.advance(10.0)
        second.exit_hold(token)  # token belongs to `first`, not `second`

        # The cross-tracker exit was a no-op: no hold closed on either tracker…
        assert recorder.closed == []
        # …the real hold on `first` is still vouching (not silently released)…
        assert first.held() is True
        # …and `second`'s own hold is untouched.
        assert second.held() is True

    def test_last_at_never_regresses_under_concurrent_writes(self) -> None:
        """The stall clock must never move backwards, even if a caller's clock
        sample is stale relative to a write another thread already committed.

        With the clock read inside the lock and the monotonic max() guard, a
        thread that sampled an earlier time cannot overwrite a later
        ``_last_at``. This pins the lenient-only accounting direction the
        ``exit_hold`` docstring promises.
        """
        clock = FakeClock()
        tracker = _tracker(clock)

        # Establish a baseline at t=10.
        clock.now = 1010.0
        tracker.mark_progress("baseline")
        assert tracker._last_at == 1010.0

        # A stale clock reading (t=5) must not move _last_at backwards.
        clock.now = 1005.0
        tracker.mark_progress("stale")
        assert tracker._last_at == 1010.0

        # The stall clock is still anchored at t=10.
        clock.now = 1015.0
        assert tracker.stalled_for() == 5.0


# ---------------------------------------------------------------------------
# Closed-hold observations (the warn-mode audit seam)
# ---------------------------------------------------------------------------


class TestClosedHoldObservations:
    def test_unbounded_hold_reports_its_observed_duration(self) -> None:
        clock = FakeClock()
        recorder = HoldRecorder()
        tracker = _tracker(clock, recorder)
        token = tracker.enter_hold("full table scan", None)

        clock.advance(4200.0)
        tracker.exit_hold(token)

        assert recorder.closed == [
            ClosedHold(
                label="full table scan",
                duration_seconds=4200.0,
                allowance_seconds=None,
            )
        ]
        observed = recorder.closed[0]
        assert observed.bounded is False
        assert observed.lapsed is False

    def test_bounded_hold_within_allowance_is_not_lapsed(self) -> None:
        clock = FakeClock()
        recorder = HoldRecorder()
        tracker = _tracker(clock, recorder)
        token = tracker.enter_hold("snapshot metadata query", 1800.0)

        clock.advance(120.0)
        tracker.exit_hold(token)

        observed = recorder.closed[0]
        assert observed.allowance_seconds == 1800.0
        assert observed.bounded is True
        assert observed.lapsed is False

    def test_bounded_hold_past_its_allowance_is_lapsed(self) -> None:
        clock = FakeClock()
        recorder = HoldRecorder()
        tracker = _tracker(clock, recorder)
        token = tracker.enter_hold("snapshot metadata query", 60.0)

        clock.advance(61.0)
        tracker.exit_hold(token)

        assert recorder.closed[0].lapsed is True

    def test_unknown_token_reports_nothing(self) -> None:
        clock = FakeClock()
        recorder = HoldRecorder()
        tracker = _tracker(clock, recorder)

        tracker.exit_hold(9999)

        assert recorder.closed == []

    def test_double_exit_reports_once_and_does_not_reset_progress(self) -> None:
        clock = FakeClock()
        recorder = HoldRecorder()
        tracker = _tracker(clock, recorder)
        token = tracker.enter_hold("full table scan", None)

        clock.advance(10.0)
        tracker.exit_hold(token)
        clock.advance(25.0)
        tracker.exit_hold(token)

        assert len(recorder.closed) == 1
        assert tracker.stalled_for() == 25.0

    def test_observer_failure_never_breaks_the_caller(self) -> None:
        clock = FakeClock()

        def explode(_: ClosedHold) -> None:
            raise RuntimeError("metric backend down")

        tracker = ProgressTracker(clock=clock, on_hold_closed=explode)
        token = tracker.enter_hold("full table scan", None)

        clock.advance(5.0)
        tracker.exit_hold(token)  # must not raise

        assert tracker.stalled_for() == 0.0

    def test_no_observer_is_fine(self) -> None:
        clock = FakeClock()
        tracker = ProgressTracker(clock=clock)
        token = tracker.enter_hold("full table scan", None)

        clock.advance(5.0)
        tracker.exit_hold(token)

        assert tracker.held() is False


# ---------------------------------------------------------------------------
# The stall verdict (what tells the cancellation handler whose cancel it is)
# ---------------------------------------------------------------------------


class TestStallVerdict:
    def test_no_verdict_until_the_watchdog_records_one(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)

        clock.advance(9000.0)

        # Quiet for hours is not a verdict: only the watchdog decides, and only
        # in enforce mode. A tracker that answered "stalled" on elapsed time
        # alone would make warn mode fail activities.
        assert tracker.stalled_for() == 9000.0
        assert tracker.stall is None

    def test_verdict_carries_the_observed_gap_and_label(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)

        tracker.flag_stalled(
            stalled_for_seconds=915.0, last_progress_label="writer.flush_buffer"
        )

        stall = tracker.stall
        assert stall is not None
        assert stall.stalled_for_seconds == 915.0
        assert stall.last_progress_label == "writer.flush_buffer"

    def test_verdict_is_frozen_against_later_progress(self) -> None:
        """The numbers must not move between the verdict and the raise site.

        The cancellation travels to the attempt's next ``await``, and anything
        still in flight can mark progress on the way — which would re-arm
        ``stalled_for()`` and relabel ``last_label``, leaving the failure to
        report a gap of nothing after a signal that arrived post-mortem.
        """
        clock = FakeClock()
        tracker = _tracker(clock)
        clock.advance(900.0)
        tracker.flag_stalled(stalled_for_seconds=900.0, last_progress_label="extract")

        clock.advance(5.0)
        tracker.mark_progress("storage.upload_part")

        stall = tracker.stall
        assert stall is not None
        assert stall.stalled_for_seconds == 900.0
        assert stall.last_progress_label == "extract"
        assert tracker.last_label == "storage.upload_part"

    def test_flagging_is_not_progress(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)
        clock.advance(600.0)

        tracker.flag_stalled(stalled_for_seconds=600.0, last_progress_label="")

        # Recording the verdict must not touch the clock it is a verdict about.
        assert tracker.stalled_for() == 600.0

    def test_first_verdict_wins(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)

        tracker.flag_stalled(stalled_for_seconds=900.0, last_progress_label="extract")
        tracker.flag_stalled(stalled_for_seconds=1800.0, last_progress_label="later")

        stall = tracker.stall
        assert stall is not None
        assert (stall.stalled_for_seconds, stall.last_progress_label) == (
            900.0,
            "extract",
        )

    def test_an_unlabelled_attempt_records_an_empty_label(self) -> None:
        clock = FakeClock()
        tracker = _tracker(clock)

        tracker.flag_stalled(stalled_for_seconds=60.0, last_progress_label="")

        stall = tracker.stall
        assert stall is not None
        assert stall.last_progress_label == ""


# ---------------------------------------------------------------------------
# Clock injection
# ---------------------------------------------------------------------------


class TestClockInjection:
    def test_defaults_to_time_monotonic(self) -> None:
        tracker = ProgressTracker()

        assert tracker._clock is time.monotonic
        assert tracker.stalled_for() >= 0.0

    def test_no_global_clock_is_read(self) -> None:
        """The injected clock is the only time source the tracker consults."""
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.mark_progress("write_batch")

        real_before = time.monotonic()
        while time.monotonic() - real_before < 0.01:
            pass

        assert tracker.stalled_for() == 0.0
