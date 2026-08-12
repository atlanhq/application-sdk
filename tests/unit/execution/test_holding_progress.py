"""Unit tests for ``holding_progress()`` (FND-291).

The context manager is four lines, so the risk is not in the code — it is in the
semantics it has to preserve under every exit path. Five things are proven here,
each a way a hold can be plausibly wrong and still look fine:

1. **The vouch works and then stops.** Inside the block the watchdog reads no
   gap even past the budget; on exit the completed call is recorded as progress.
2. **The lapse resumes from the deadline, not from the last signal before the
   hold.** That is what makes the effective kill time for a wedged held call
   ``timeout + budget`` (ADR-0018 → *Holds*). Resuming from the earlier signal
   would fire the stall the instant the allowance lapsed; resuming from "now"
   would forgive the allowance twice.
3. **Every exit path releases.** Return, exception and cancellation. A leaked
   hold does not fail loudly — it silently pauses the watchdog for the rest of
   the attempt, which is the exact failure the watchdog exists to catch.
4. **Concurrent and nested blocks cannot release each other.** Holds are keyed
   by token rather than stacked, and ``asyncio.gather`` over several opaque
   calls is the shape that would break a stack.
5. **The SDK never derives or defaults the allowance.** Pinned against the
   signature, because a default added later would silently reintroduce the
   guessed duration knob ADR-0018 exists to abolish.

Clocks are injected, never patched: an asyncio loop shares ``time.monotonic``,
so patching it globally makes the loop itself misbehave.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator

import pytest

from application_sdk.app.base import App, AppContextError
from application_sdk.app.context import AppContext, TaskExecutionContext
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.heartbeat import NoopHeartbeatController
from application_sdk.execution.progress import (
    ClosedHold,
    ProgressTracker,
    bind_progress_tracker,
    current_progress_tracker,
    holding_progress,
)

BUDGET = 300.0
"""A stand-in for ``max_no_progress_seconds``. Only used in comparisons."""


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def closed() -> list[ClosedHold]:
    """Every hold this test closes, in order — the FND-292 audit seam."""
    return []


@pytest.fixture
def tracker(clock: _Clock, closed: list[ClosedHold]) -> Iterator[ProgressTracker]:
    """A real tracker bound as the current attempt's, as ``activities.py`` does."""
    instance = ProgressTracker(clock=clock, on_hold_closed=closed.append)
    with bind_progress_tracker(instance):
        yield instance


class TestTheVouch:
    """Inside the block there is no gap; outside it the call counts as progress."""

    @pytest.mark.asyncio
    async def test_a_bounded_hold_suppresses_a_gap_past_the_budget(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        async with holding_progress("snapshot metadata query", timeout=1800):
            clock.advance(BUDGET * 3)

            assert tracker.held() is True
            assert tracker.stalled_for() == 0.0

    @pytest.mark.asyncio
    async def test_exiting_records_the_call_as_progress(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        """A completed opaque call *is* forward progress, under its own label.

        Without this the attempt would leave the block already at the gap it
        spent inside it, and stall on the very next tick.
        """
        async with holding_progress("snapshot metadata query", timeout=1800):
            clock.advance(1500.0)

        assert tracker.held() is False
        assert tracker.stalled_for() == 0.0
        assert tracker.last_label == "snapshot metadata query"

    @pytest.mark.asyncio
    async def test_the_gap_resumes_from_the_exit(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        async with holding_progress("snapshot metadata query", timeout=1800):
            clock.advance(1500.0)
        clock.advance(60.0)

        assert tracker.stalled_for() == pytest.approx(60.0)

    @pytest.mark.asyncio
    async def test_an_unbounded_hold_vouches_indefinitely(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        """``timeout=None`` is the declared residual: the backstop is the bound."""
        async with holding_progress("vendor export", timeout=None):
            clock.advance(86_400.0)

            assert tracker.held() is True
            assert tracker.stalled_for() == 0.0

    @pytest.mark.asyncio
    async def test_the_observed_hold_is_reported_once(
        self, tracker: ProgressTracker, clock: _Clock, closed: list[ClosedHold]
    ) -> None:
        """What warn mode ranks sites on (FND-292)."""
        async with holding_progress("snapshot metadata query", timeout=1800):
            clock.advance(400.0)

        assert closed == [
            ClosedHold(
                label="snapshot metadata query",
                duration_seconds=400.0,
                allowance_seconds=1800.0,
            )
        ]
        assert closed[0].bounded is True
        assert closed[0].lapsed is False


class TestTheLapse:
    """Past the allowance the hold stops vouching and the watchdog resumes."""

    @pytest.mark.asyncio
    async def test_the_hold_stops_vouching_at_its_deadline(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        async with holding_progress("snapshot metadata query", timeout=1800):
            clock.advance(1799.0)
            assert tracker.held() is True

            clock.advance(2.0)
            assert tracker.held() is False

    @pytest.mark.asyncio
    async def test_the_gap_resumes_from_the_deadline_not_the_entry(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        """The kill time for a wedged held call is ``timeout + budget``.

        The allowance vouched for everything up to the deadline, so the earliest
        instant the attempt can be accused of stalling is the deadline itself —
        not the last progress signal before the hold, which would fire the stall
        the moment the allowance lapsed.
        """
        async with holding_progress("snapshot metadata query", timeout=1800):
            clock.advance(1800.0 + BUDGET - 1.0)
            assert tracker.stalled_for() < BUDGET

            clock.advance(2.0)
            assert tracker.stalled_for() >= BUDGET

    @pytest.mark.asyncio
    async def test_a_lapsed_hold_is_reported_as_lapsed(
        self, tracker: ProgressTracker, clock: _Clock, closed: list[ClosedHold]
    ) -> None:
        """A too-tight allowance is visible in the audit, not only in a kill."""
        async with holding_progress("snapshot metadata query", timeout=1800):
            clock.advance(2000.0)

        assert closed[0].lapsed is True
        assert closed[0].duration_seconds == pytest.approx(2000.0)

    @pytest.mark.asyncio
    async def test_a_negative_allowance_vouches_for_nothing(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        """A programming error must not forgive the quiet that preceded it.

        ``enter_hold`` logs and treats it as already exhausted; the block still
        runs and still releases, because a bad number is not a reason to change
        what the app's code does.
        """
        clock.advance(BUDGET + 1.0)

        async with holding_progress("snapshot metadata query", timeout=-1.0):
            assert tracker.held() is False
            assert tracker.stalled_for() >= BUDGET


class TestEveryExitPathReleases:
    """A leaked hold pauses the watchdog for the rest of the attempt, silently."""

    @pytest.mark.asyncio
    async def test_an_exception_releases_the_hold_and_propagates(
        self, tracker: ProgressTracker
    ) -> None:
        with pytest.raises(ValueError, match="source refused"):
            async with holding_progress("snapshot metadata query", timeout=1800):
                raise ValueError("source refused")

        assert tracker.held() is False

    @pytest.mark.asyncio
    async def test_a_cancellation_releases_the_hold(
        self, tracker: ProgressTracker
    ) -> None:
        """The shape a Temporal activity cancellation actually takes."""
        inside = asyncio.Event()

        async def _held_forever() -> None:
            async with holding_progress("snapshot metadata query", timeout=1800):
                inside.set()
                await asyncio.Event().wait()

        running = asyncio.create_task(_held_forever())
        await inside.wait()
        assert tracker.held() is True

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        assert tracker.held() is False

    @pytest.mark.asyncio
    async def test_a_hold_still_reports_when_the_body_raises(
        self, tracker: ProgressTracker, clock: _Clock, closed: list[ClosedHold]
    ) -> None:
        """A site that fails is a site the work-list needs most."""
        with pytest.raises(ValueError):
            async with holding_progress("snapshot metadata query", timeout=1800):
                clock.advance(120.0)
                raise ValueError("source refused")

        assert [(c.label, c.duration_seconds) for c in closed] == [
            ("snapshot metadata query", 120.0)
        ]

    @pytest.mark.asyncio
    async def test_an_unentered_context_manager_opens_no_hold(
        self, tracker: ProgressTracker
    ) -> None:
        """Building the manager without ``async with`` must not leak a hold.

        The hold opens in ``__aenter__``, so a manager that is constructed and
        dropped — a refactor that loses the ``async with`` — vouches for
        nothing rather than vouching forever.
        """
        holding_progress("snapshot metadata query", timeout=1800)

        assert tracker.held() is False


class TestConcurrentAndNested:
    """Holds are keyed by token, so no block can release another's deadline."""

    @pytest.mark.asyncio
    async def test_gathered_holds_release_only_their_own(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        both_inside = asyncio.Barrier(2)
        first_released = asyncio.Event()

        async def _fast() -> None:
            async with holding_progress("small list call", timeout=60):
                await both_inside.wait()
            first_released.set()

        async def _slow() -> None:
            async with holding_progress("full table scan", timeout=7200):
                await both_inside.wait()
                await first_released.wait()
                # The fast block has exited. A stack-based implementation would
                # have popped this hold instead of its own.
                clock.advance(BUDGET * 2)
                assert tracker.held() is True
                assert tracker.stalled_for() == 0.0

        await asyncio.gather(_fast(), _slow())

        assert tracker.held() is False

    @pytest.mark.asyncio
    async def test_nesting_leaves_the_outer_hold_vouching(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        async with holding_progress("full table scan", timeout=7200):
            async with holding_progress("row count probe", timeout=60):
                pass

            clock.advance(BUDGET * 2)
            assert tracker.held() is True
            assert tracker.stalled_for() == 0.0

    @pytest.mark.asyncio
    async def test_each_gathered_hold_is_reported_separately(
        self, tracker: ProgressTracker, closed: list[ClosedHold]
    ) -> None:
        async def _hold(label: str) -> None:
            async with holding_progress(label, timeout=60):
                await asyncio.sleep(0)

        await asyncio.gather(_hold("call-a"), _hold("call-b"), _hold("call-c"))

        assert sorted(c.label for c in closed) == ["call-a", "call-b", "call-c"]


class TestTheAllowanceIsNeverDefaulted:
    """FND-291's acceptance criterion, pinned where a refactor would break it."""

    def test_timeout_is_required_and_keyword_only(self) -> None:
        parameter = inspect.signature(holding_progress).parameters["timeout"]

        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    @pytest.mark.parametrize(
        "delegate",
        [TaskExecutionContext.holding_progress, App.holding_progress],
        ids=["task-context", "app"],
    )
    def test_the_delegates_do_not_default_it_either(self, delegate: object) -> None:
        parameter = inspect.signature(delegate).parameters["timeout"]  # type: ignore[arg-type]

        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    def test_omitting_the_allowance_is_a_type_error(self) -> None:
        with pytest.raises(TypeError, match="timeout"):
            holding_progress("snapshot metadata query")  # type: ignore[call-arg]


class TestOutsideAnActivity:
    """Local runs, unit tests and scripts behave as they did before ADR-0018."""

    @pytest.mark.asyncio
    async def test_the_block_is_inert_with_no_tracker_bound(self) -> None:
        async with holding_progress("snapshot metadata query", timeout=1800):
            assert current_progress_tracker().held() is False

        assert current_progress_tracker().stalled_for() == 0.0
        assert current_progress_tracker().last_label == ""

    @pytest.mark.asyncio
    async def test_an_inert_block_swallows_nothing(self) -> None:
        with pytest.raises(ValueError, match="source refused"):
            async with holding_progress("snapshot metadata query", timeout=1800):
                raise ValueError("source refused")


class _HoldIn(Input, allow_unbounded_fields=True):
    name: str = "default"


class _HoldOut(Output, allow_unbounded_fields=True):
    held_for: float = 0.0


def _task_execution_context() -> TaskExecutionContext:
    """A ``TaskExecutionContext`` with no Temporal behind it."""
    return TaskExecutionContext(
        app_context=AppContext(app_name="_hold-app", app_version="0.0.0"),
        task_name="scan",
        heartbeat_controller=NoopHeartbeatController(),
    )


class _HoldApp(App):
    """The minimum an ``App`` needs to exist; the delegate is what's under test."""

    async def run(self, input: _HoldIn) -> _HoldOut:
        return _HoldOut()


class TestTheDelegates:
    """The two receivers app authors actually type."""

    @pytest.mark.asyncio
    async def test_the_task_context_holds_on_the_attempts_tracker(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        async with _task_execution_context().holding_progress(
            "full table scan", timeout=7200
        ):
            clock.advance(BUDGET * 2)

            assert tracker.held() is True
            assert tracker.stalled_for() == 0.0

        assert tracker.last_label == "full table scan"

    @pytest.mark.asyncio
    async def test_the_app_delegate_reaches_the_same_hold(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        """``self.holding_progress(...)``, alongside ``self.run_in_thread(...)``."""
        app = _HoldApp()
        app._task_context = _task_execution_context()

        async with app.holding_progress("full table scan", timeout=7200):
            clock.advance(BUDGET * 2)

            assert current_progress_tracker() is tracker
            assert tracker.stalled_for() == 0.0

        assert tracker.last_label == "full table scan"

    def test_the_app_delegate_refuses_outside_a_task(self) -> None:
        """A hold in ``run()`` would claim coverage no watchdog provides."""
        with pytest.raises(AppContextError, match="inside @task methods"):
            _HoldApp().holding_progress("full table scan", timeout=7200)


class TestTheAdrsBlockingExample:
    """``holding_progress`` around ``run_in_thread`` — the ADR's second example."""

    @pytest.mark.asyncio
    async def test_the_hold_spans_the_offload(
        self, tracker: ProgressTracker, clock: _Clock
    ) -> None:
        def _blocking_scan() -> str:
            # Read from inside the worker thread: the hold was opened on the
            # loop, so this proves the offload sees the same vouched attempt
            # rather than a private copy that dies with the thread.
            assert current_progress_tracker() is tracker
            assert tracker.held() is True
            return "scanned"

        context = _task_execution_context()

        async with context.holding_progress("full table scan", timeout=7200):
            clock.advance(BUDGET * 2)
            assert await context.run_in_thread(_blocking_scan) == "scanned"

        assert tracker.held() is False
        assert tracker.last_label == "full table scan"
