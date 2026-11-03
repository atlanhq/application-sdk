"""Tests for the shared backoff policy and retry loop."""

from __future__ import annotations

import pytest

from application_sdk.common.backoff import Backoff, Jitter, RetryExhausted, retry_async


class _Transient(Exception):
    pass


class _Fatal(Exception):
    pass


class _Recorder:
    """Records sleeps and advances a fake clock by each one."""

    def __init__(self, per_call: float = 0.0) -> None:
        self.sleeps: list[float] = []
        self.now = 0.0
        self.per_call = per_call

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay

    def clock(self) -> float:
        return self.now


def _failing(times: int, exc: type[Exception] = _Transient):
    calls = {"n": 0}

    async def call() -> str:
        calls["n"] += 1
        if calls["n"] <= times:
            raise exc("x")
        return "ok"

    return call, calls


class TestBackoffShape:
    def test_geometric_ladder_is_capped(self) -> None:
        b = Backoff(base=0.5, cap=5.0, jitter=Jitter.NONE)
        assert [b.delay(n) for n in range(1, 7)] == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]

    def test_steps_repeat_the_last_one_past_the_end(self) -> None:
        b = Backoff(steps=(5.0, 10.0, 20.0), jitter=Jitter.NONE)
        assert [b.delay(n) for n in range(1, 5)] == [5.0, 10.0, 20.0, 20.0]

    def test_huge_retry_number_does_not_overflow(self) -> None:
        assert Backoff(base=1.0, cap=30.0, jitter=Jitter.NONE).delay(10_000) == 30.0

    def test_full_jitter_stays_under_the_nominal_delay(self) -> None:
        b = Backoff(base=4.0, factor=1.0)
        draws = [b.delay(1) for _ in range(500)]
        assert all(0.0 <= d <= 4.0 for d in draws)
        assert len(set(draws)) > 1

    def test_spread_jitter_stays_within_half_to_one_and_a_half(self) -> None:
        b = Backoff(base=10.0, factor=1.0, jitter=Jitter.SPREAD)
        draws = [b.delay(1) for _ in range(500)]
        assert all(5.0 <= d <= 15.0 for d in draws)

    def test_retries_is_the_tighter_of_steps_and_max_attempts(self) -> None:
        assert Backoff(steps=(1.0, 2.0)).retries == 2
        assert Backoff(steps=(1.0, 2.0), max_attempts=2).retries == 1
        assert Backoff(max_attempts=4).retries == 3
        assert Backoff(budget_seconds=10).retries is None

    @pytest.mark.parametrize(
        "kwargs",
        [{"max_attempts": 0}, {"base": -1.0}, {"steps": (1.0, -1.0)}, {"cap": -1.0}],
    )
    def test_invalid_shapes_are_rejected(self, kwargs) -> None:
        with pytest.raises(ValueError):
            Backoff(**kwargs)

    def test_retry_number_is_one_based(self) -> None:
        with pytest.raises(ValueError):
            Backoff(base=1.0).delay(0)


class TestRetryAsync:
    async def test_retries_then_succeeds(self) -> None:
        rec = _Recorder()
        call, calls = _failing(2)
        result = await retry_async(
            call,
            backoff=Backoff(steps=(1.0, 2.0, 3.0), jitter=Jitter.NONE),
            retry_if=lambda e: isinstance(e, _Transient),
            sleep=rec.sleep,
        )
        assert result == "ok"
        assert calls["n"] == 3
        assert rec.sleeps == [1.0, 2.0]

    async def test_a_non_retryable_error_propagates_at_once(self) -> None:
        rec = _Recorder()
        call, calls = _failing(5, _Fatal)
        with pytest.raises(_Fatal):
            await retry_async(
                call,
                backoff=Backoff(max_attempts=5, jitter=Jitter.NONE),
                retry_if=lambda e: isinstance(e, _Transient),
                sleep=rec.sleep,
            )
        assert calls["n"] == 1
        assert rec.sleeps == []

    async def test_max_attempts_counts_the_first_call(self) -> None:
        rec = _Recorder()
        call, calls = _failing(10)
        with pytest.raises(_Transient):
            await retry_async(
                call,
                backoff=Backoff(base=1.0, max_attempts=3, jitter=Jitter.NONE),
                retry_if=lambda _e: True,
                sleep=rec.sleep,
            )
        assert calls["n"] == 3
        assert rec.sleeps == [1.0, 2.0]

    async def test_budget_clamps_the_last_delay_and_stops(self) -> None:
        rec = _Recorder()
        call, calls = _failing(100)
        with pytest.raises(_Transient):
            await retry_async(
                call,
                backoff=Backoff(
                    base=4.0, factor=1.0, jitter=Jitter.NONE, budget_seconds=10
                ),
                retry_if=lambda _e: True,
                sleep=rec.sleep,
                clock=rec.clock,
            )
        # 4 + 4 + 2 (clamped to what remains), then the budget is spent.
        assert rec.sleeps == [4.0, 4.0, 2.0]
        assert calls["n"] == 4

    async def test_on_giveup_replaces_the_terminal_error(self) -> None:
        rec = _Recorder()
        call, _ = _failing(10)
        seen: list[RetryExhausted] = []

        def giveup(ex: RetryExhausted) -> Exception:
            seen.append(ex)
            return _Fatal(f"gave up after {ex.attempts}")

        with pytest.raises(_Fatal) as info:
            await retry_async(
                call,
                backoff=Backoff(steps=(1.0,), jitter=Jitter.NONE),
                retry_if=lambda _e: True,
                on_giveup=giveup,
                sleep=rec.sleep,
                clock=rec.clock,
            )
        assert isinstance(info.value.__cause__, _Transient)
        assert seen[0].attempts == 2
        assert seen[0].elapsed_seconds == 1.0

    async def test_on_retry_sees_attempt_error_and_delay(self) -> None:
        rec = _Recorder()
        call, _ = _failing(2)
        seen: list[tuple[int, str, float]] = []
        await retry_async(
            call,
            backoff=Backoff(steps=(1.0, 2.0), jitter=Jitter.NONE),
            retry_if=lambda _e: True,
            on_retry=lambda n, e, d: seen.append((n, type(e).__name__, d)),
            sleep=rec.sleep,
        )
        assert seen == [(1, "_Transient", 1.0), (2, "_Transient", 2.0)]

    async def test_an_unbounded_backoff_is_refused(self) -> None:
        call, calls = _failing(0)
        with pytest.raises(ValueError):
            await retry_async(call, backoff=Backoff(base=1.0), retry_if=lambda _e: True)
        assert calls["n"] == 0
