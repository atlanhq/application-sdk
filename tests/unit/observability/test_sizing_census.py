"""Unit tests for the concurrency census."""

from __future__ import annotations

import threading

import pytest

from application_sdk.observability.sizing_census import CENSUS


@pytest.fixture(autouse=True)
def clean():
    CENSUS._reset_for_testing()
    yield
    CENSUS._reset_for_testing()


class TestCensus:
    def test_alone_is_one(self):
        token, now = CENSUS.enter()
        assert now == 1
        assert CENSUS.leave(token) == 1

    def test_counts_overlap(self):
        a, _ = CENSUS.enter()
        b, now_b = CENSUS.enter()
        assert now_b == 2
        assert CENSUS.leave(b) == 2
        assert CENSUS.leave(a) == 2

    def test_reports_the_high_water_mark_not_the_entry_count(self):
        """An activity that started alone and was joined has a pod-wide peak."""
        a, now_a = CENSUS.enter()
        assert now_a == 1  # A genuinely was alone at entry
        b, _ = CENSUS.enter()
        CENSUS.leave(b)
        assert CENSUS.leave(a) == 2  # ...but not for its whole window

    def test_a_later_arrival_is_not_credited_with_earlier_peaks(self):
        """C started after the crowd left, so C's window really was quiet."""
        a, _ = CENSUS.enter()
        b, _ = CENSUS.enter()
        CENSUS.leave(a)
        CENSUS.leave(b)
        c, now_c = CENSUS.enter()
        assert now_c == 1
        assert CENSUS.leave(c) == 1

    def test_count_returns_to_zero(self):
        a, _ = CENSUS.enter()
        b, _ = CENSUS.enter()
        CENSUS.leave(a)
        CENSUS.leave(b)
        assert CENSUS.active() == 0

    def test_unknown_token_is_tolerated(self):
        """A double-leave must not corrupt the count for everyone else."""
        a, _ = CENSUS.enter()
        assert CENSUS.leave(a) == 1
        assert CENSUS.leave(a) == 1  # already gone
        assert CENSUS.active() == 0

    def test_thread_safe(self):
        """Activities run on the event loop, but the executor path is threaded."""
        results: list[int] = []
        lock = threading.Lock()

        def work():
            token, _ = CENSUS.enter()
            seen = CENSUS.leave(token)
            with lock:
                results.append(seen)

        threads = [threading.Thread(target=work) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 40
        assert all(r >= 1 for r in results)
        assert CENSUS.active() == 0


class TestPeakAndIdempotentLeave:
    def test_peak_does_not_deregister(self):
        """The caller needs its number while still holding its slot."""
        a, _ = CENSUS.enter()
        b, _ = CENSUS.enter()
        assert CENSUS.peak(a) == 2
        assert CENSUS.active() == 2  # peak() must not have released anything
        CENSUS.leave(a)
        CENSUS.leave(b)

    def test_leave_is_idempotent(self):
        """Two callers deregister each execution; decrementing twice undercounts."""
        a, _ = CENSUS.enter()
        b, _ = CENSUS.enter()
        assert CENSUS.leave(a) == 2
        assert CENSUS.leave(a) == 1  # second call is a no-op
        assert CENSUS.active() == 1  # b is still running and still counted
        CENSUS.leave(b)
        assert CENSUS.active() == 0

    def test_peak_for_an_unknown_token(self):
        assert CENSUS.peak(99999) == 1
