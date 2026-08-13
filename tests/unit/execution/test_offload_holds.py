"""Unit tests for the ``run_in_thread`` / ``run_fault_isolated`` auto-holds (FND-290).

ADR-0018 → *Feeding the tracker*, mechanism 2. The one thing these tests exist to
prove is a **negative**: turning the stall watchdog on must not make a
legitimately long blocking call fail where it used to succeed. So the assertions
are about the hold being *unbounded*, being active for the call's whole duration,
and — the way this would actually break in production — being released by
whichever offload owns it and no other.

Two properties get counted rather than merely observed, because presence alone
would pass while the mechanism was wrong:

- **Exactly one hold per offload.** Three public entry points funnel into one
  implementation, so a hold added per entry point instead of per offload would
  still show "a hold is active" while double-counting every call the wrapper
  makes.
- **Nothing in a label but code.** A hold label reaches the stall log, the metric
  and the warn-mode report, so an argument leaking into one is a data leak, not a
  cosmetic bug.

The fake clock is injected into the tracker, never patched globally: an asyncio
loop shares ``time.monotonic``, and patching it makes the loop itself misbehave.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from application_sdk.app.context import AppContext, TaskExecutionContext
from application_sdk.execution.heartbeat import (
    NoopHeartbeatController,
    _check_for_stall,
    run_best_effort,
    run_fault_isolated,
    run_in_thread,
)
from application_sdk.execution.progress import (
    ProgressWatchdogMode,
    bind_progress_tracker,
    current_progress_tracker,
    holding_progress,
)
from tests.unit.conftest import RecordingProgressTracker

THREAD_PREFIX = "run_in_thread."
ISOLATED_PREFIX = "run_fault_isolated."
UNNAMEABLE = "<callable>"


@dataclass
class _FakeClock:
    """A monotonic clock the test advances explicitly."""

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def tracker() -> Any:
    """Bind a recording tracker for the test, as ``activities.py`` would."""
    recording = RecordingProgressTracker()
    with bind_progress_tracker(recording):
        yield recording


# Module-level so the spawn child can pickle them by reference (it re-imports
# this module), which rules out closures and mocks for the isolated tests.


def _echo(value: str) -> str:
    return f"echo:{value}"


def _sleep_forever() -> None:
    time.sleep(3600)


def _raise_value_error() -> None:
    raise ValueError("boom")


class _Cursor:
    """Stands in for the shape the ADR calls dominant: ``cursor.execute(sql)``."""

    def execute(self, sql: str) -> str:
        return f"rows for {sql}"


class _Callable:
    """A callable *instance*: its name lives on the class, not the object."""

    def __call__(self) -> str:
        return "called"


# ---------------------------------------------------------------------------
# The offload is vouched for, through every entry point
# ---------------------------------------------------------------------------


class TestTheOffloadIsVouchedFor:
    @pytest.mark.asyncio
    async def test_the_hold_is_active_for_the_whole_call(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """Read from inside the blocking call: the vouch is live *while* it runs.

        Asserting only on the closed hold afterwards would pass even if the hold
        were entered and released back to back around the offload.
        """

        def _blocking() -> tuple[bool, float]:
            return tracker.held(), tracker.stalled_for()

        held_during, stalled_during = await run_in_thread(_blocking)

        assert held_during is True
        assert stalled_during == 0.0
        assert tracker.held() is False, "the hold must not outlive the offload"

    @pytest.mark.asyncio
    async def test_the_context_wrapper_reaches_the_same_hold(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """``TaskExecutionContext.run_in_thread`` — the entry point apps use."""
        context = TaskExecutionContext(
            app_context=AppContext(app_name="_hold-app", app_version="0.0.0"),
            task_name="extract",
            heartbeat_controller=NoopHeartbeatController(),
        )

        def _blocking() -> bool:
            return tracker.held()

        assert await context.run_in_thread(_blocking) is True
        assert len(tracker.holds) == 1
        assert tracker.holds[0].label.startswith(THREAD_PREFIX)

    @pytest.mark.asyncio
    async def test_one_hold_per_offload_not_per_entry_point(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """A count, because every layering mistake here still looks vouched-for.

        The module-level function, the ``TaskExecutionContext`` wrapper and
        ``App.run_in_thread`` are three public entry points over *one*
        implementation. Holding at each layer instead of at the offload would
        report two or three holds for a single blocking call — and would inflate
        the warn-mode duration ranking with holds nobody can act on.
        """
        context = TaskExecutionContext(
            app_context=AppContext(app_name="_hold-app", app_version="0.0.0"),
            task_name="extract",
            heartbeat_controller=NoopHeartbeatController(),
        )

        await run_in_thread(_echo, "a")
        await context.run_in_thread(_echo, "b")

        assert len(tracker.holds) == 2

    @pytest.mark.asyncio
    async def test_a_completed_offload_counts_as_progress(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """A loop of short offloads keeps the attempt alive on its own.

        ``exit_hold`` re-arms the stall clock under the hold's label, so a task
        whose only observable work is a sequence of blocking calls needs no
        framework hook and no manual heartbeat to stay out of the watchdog.
        """
        for _ in range(3):
            await run_in_thread(_echo, "chunk")

        assert tracker.last_label == f"{THREAD_PREFIX}_echo"
        assert tracker.stalled_for() < 1.0


# ---------------------------------------------------------------------------
# Unbounded: the upgrade must never false-kill
# ---------------------------------------------------------------------------


class TestTheHoldIsUnbounded:
    @pytest.mark.asyncio
    async def test_no_allowance_is_invented(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """The SDK declares nothing on the author's behalf.

        Deriving a bound from the call's own ``timeout=`` kwarg was rejected in
        review — it is per-operation, so it is systematically smaller than the
        call's legitimate duration. Passing one here must change nothing.
        """
        await run_in_thread(_echo, "value")
        await run_in_thread(lambda timeout: timeout, timeout=30)

        assert [hold.allowance_seconds for hold in tracker.holds] == [None, None]
        assert [hold.bounded for hold in tracker.holds] == [False, False]

    @pytest.mark.asyncio
    async def test_a_six_hour_blocking_call_is_not_a_stall(self) -> None:
        """The whole point of the issue, asserted through the watchdog itself.

        Six hours pass inside one blocking call — longer than the largest
        ``start_to_close`` weight class in the fleet — against a 60s budget in
        ``ENFORCE``. The watchdog must not fire, so upgrading a task that makes
        one long blocking call cannot fail where it used to succeed.
        """
        clock = _FakeClock()
        recording = RecordingProgressTracker(clock=clock)
        on_stall = MagicMock()

        def _six_hours_of_blocking_work() -> None:
            clock.advance(6 * 60 * 60)

        with bind_progress_tracker(recording):
            await run_in_thread(_six_hours_of_blocking_work)
            # Checked after the call as well: a hold that stopped vouching the
            # moment it closed would leave the six quiet hours it covered on the
            # clock, and the very next tick would kill the attempt.
            fired = _check_for_stall(
                progress=recording,
                budget_seconds=60.0,
                mode=ProgressWatchdogMode.ENFORCE,
                task_name="extract",
                on_stall=on_stall,
            )

        assert fired is False
        on_stall.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_watchdog_is_inactive_mid_call_by_design(self) -> None:
        """The accepted residual, pinned so it is a decision and not a surprise.

        An unbounded hold means the stall watchdog cannot fire *at all* while the
        call runs, however long it runs — the duration backstop is the only bound
        left. If this ever starts failing, someone has narrowed the residual, and
        ADR-0018 plus the warn-mode report need to be re-read before it lands.
        """
        clock = _FakeClock()
        recording = RecordingProgressTracker(clock=clock)
        on_stall = MagicMock()

        def _wedged_blocking_call() -> bool:
            clock.advance(24 * 60 * 60)
            return _check_for_stall(
                progress=recording,
                budget_seconds=60.0,
                mode=ProgressWatchdogMode.ENFORCE,
                task_name="extract",
                on_stall=on_stall,
            )

        with bind_progress_tracker(recording):
            fired = await run_in_thread(_wedged_blocking_call)

        assert fired is False
        on_stall.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_hold_reports_the_duration_it_observed(self) -> None:
        """What makes an auto-held call visible to the warn-mode work-list.

        The hold is the reason the watchdog says nothing, so without the observed
        duration on the way out, blocking work would be invisible to the audit
        precisely *because* it was vouched for.
        """
        clock = _FakeClock()
        recording = RecordingProgressTracker(clock=clock)

        with bind_progress_tracker(recording):
            await run_in_thread(lambda: clock.advance(90.0))

        assert len(recording.holds) == 1
        closed = recording.holds[0]
        assert closed.duration_seconds == pytest.approx(90.0)
        assert closed.bounded is False
        assert closed.lapsed is False, "an unbounded hold can never lapse"


# ---------------------------------------------------------------------------
# Release discipline: only ever this offload's own token
# ---------------------------------------------------------------------------


class TestTheHoldIsAlwaysReleased:
    @pytest.mark.asyncio
    async def test_released_when_the_blocking_call_raises(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """A leak here silences the watchdog for the rest of the attempt."""
        with pytest.raises(ValueError, match="boom"):
            await run_in_thread(_raise_value_error)

        assert tracker.held() is False
        assert len(tracker.holds) == 1

    @pytest.mark.asyncio
    async def test_released_when_the_caller_is_cancelled_mid_offload(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """Cancellation is the realistic leak: the thread outlives the await.

        A cancelled activity that later resumes work in the same context — or a
        cancellation swallowed by a caller — would otherwise carry an unbounded
        hold nobody can release, and the watchdog would never fire again.
        """
        started = threading.Event()
        release = threading.Event()

        def _blocking() -> None:
            started.set()
            release.wait(timeout=5)

        offload = asyncio.create_task(run_in_thread(_blocking))
        # Asserted, not awaited-and-discarded: a `wait` that timed out on a
        # contended runner would cancel before the offload owned its hold, and
        # the test would pass without ever exercising the release it exists for.
        assert await asyncio.to_thread(started.wait, 5), "offload never started"
        offload.cancel()
        with pytest.raises(asyncio.CancelledError):
            await offload
        release.set()

        assert tracker.held() is False
        assert len(tracker.holds) == 1

    @pytest.mark.asyncio
    async def test_concurrent_offloads_release_only_their_own_hold(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """``asyncio.gather`` over several offloads — the shape holds are keyed for.

        The fast offload finishes first. If holds were stacked rather than keyed
        by token, its release would pop the slow one's entry and the attempt
        would stop being vouched for while the slow call is still blocking.
        """
        release_slow = threading.Event()
        slow_running = threading.Event()

        def _slow() -> str:
            slow_running.set()
            release_slow.wait(timeout=5)
            return "slow"

        def _fast() -> str:
            return "fast"

        slow = asyncio.create_task(run_in_thread(_slow))
        # Asserted for the same reason as the cancellation test: if the slow
        # offload never got going, "the fast release left a hold standing" would
        # be checking nothing.
        assert await asyncio.to_thread(slow_running.wait, 5), "slow never started"

        assert await run_in_thread(_fast) == "fast"
        assert (
            tracker.held() is True
        ), "the fast offload's release must not take the slow one's hold with it"

        release_slow.set()
        assert await slow == "slow"
        assert tracker.held() is False
        assert len(tracker.holds) == 2


# ---------------------------------------------------------------------------
# A declared allowance wins: the auto-hold stands down inside holding_progress
# ---------------------------------------------------------------------------


class TestADeclaredAllowanceGoverns:
    """``holding_progress`` around an offload — ADR-0018's blocking example.

    The tracker's hold set is a *union*: ``held()`` and ``stalled_for()`` report
    vouched while any hold is unlapsed, and an unbounded hold never lapses. So an
    auto-hold nested inside a declared one would keep vouching past the declared
    allowance and hand the site back to the 24h backstop — the exact outcome
    ``holding_progress`` exists to prevent. The auto-hold therefore stands down
    inside a declared block.
    """

    @pytest.mark.asyncio
    async def test_a_wedged_offload_is_still_caught_at_timeout_plus_budget(
        self,
    ) -> None:
        """The promise `holding_progress`'s own docstring makes, end to end.

        A blocking call wedges inside a declared 7200s allowance. Once the
        allowance lapses the watchdog must resume *from the deadline* and fire
        one budget later — not stay paused for the 24h backstop because the
        offload added a vouch nobody asked for.
        """
        clock = _FakeClock()
        recording = RecordingProgressTracker(clock=clock)
        on_stall = MagicMock()
        wedged = threading.Event()
        entered = threading.Event()

        def _wedged_blocking_call() -> None:
            entered.set()
            wedged.wait(timeout=10)

        try:
            with bind_progress_tracker(recording):
                async with holding_progress("full table scan", timeout=7200):
                    offload = asyncio.create_task(run_in_thread(_wedged_blocking_call))
                    assert await asyncio.to_thread(entered.wait, 5), "never started"

                    # The declared allowance lapses, then the budget elapses.
                    clock.advance(7200 + 60 + 1)

                    assert recording.held() is False
                    assert recording.stalled_for() == pytest.approx(61.0)
                    fired = _check_for_stall(
                        progress=recording,
                        budget_seconds=60.0,
                        mode=ProgressWatchdogMode.ENFORCE,
                        task_name="extract",
                        on_stall=on_stall,
                    )

                    assert fired is True
                    on_stall.assert_called_once()
        finally:
            wedged.set()
            await offload

    @pytest.mark.asyncio
    async def test_only_the_declared_hold_is_recorded(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """One hold around the offload, not two.

        The warn-mode work-list should rank the site an author can act on — the
        declared one — rather than double-counting the same wall-clock under a
        second auto-generated label.
        """
        async with holding_progress("full table scan", timeout=7200):
            await run_in_thread(_echo, "page")

            assert tracker.held() is True

        assert [hold.label for hold in tracker.holds] == ["full table scan"]
        assert [hold.allowance_seconds for hold in tracker.holds] == [7200]
        assert tracker.held() is False

    @pytest.mark.asyncio
    async def test_an_isolated_call_stands_down_too(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """Both offload seams obey the same precedence rule.

        A rule that held at one seam and not the other would be worse than no
        rule: the same `holding_progress` block would mean different things
        depending on which primitive the body happened to reach for.
        """
        async with holding_progress("validation scan", timeout=600):
            await run_fault_isolated(_echo, "hi", timeout=30)

        assert [hold.label for hold in tracker.holds] == ["validation scan"]

    @pytest.mark.asyncio
    async def test_a_concurrent_offload_outside_the_block_is_still_held(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """The reason this is context-scoped and not tracker-scoped.

        Suppressing on "some hold is open on this tracker" would leave a
        concurrent offload in an unrelated task unvouched for its whole duration
        — re-introducing the false-kill the auto-hold exists to remove. Only the
        task that entered the block sees the mark.
        """
        inside_running = threading.Event()
        release_inside = threading.Event()

        async def _inside_a_declared_block() -> None:
            async with holding_progress("declared", timeout=600):
                await run_in_thread(inside_running.set)
                await asyncio.to_thread(release_inside.wait, 5)

        declared = asyncio.create_task(_inside_a_declared_block())
        assert await asyncio.to_thread(inside_running.wait, 5), "never started"

        # This offload's task never entered the block, so it must be auto-held.
        await run_in_thread(_echo, "elsewhere")

        release_inside.set()
        await declared

        labels = [hold.label for hold in tracker.holds]
        assert (
            f"{THREAD_PREFIX}_echo" in labels
        ), "an offload outside the declared block must still be vouched for"
        assert "declared" in labels

    @pytest.mark.asyncio
    async def test_the_auto_hold_returns_once_the_block_exits(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """Standing down lasts exactly as long as the declared allowance does."""
        async with holding_progress("full table scan", timeout=7200):
            await run_in_thread(_echo, "inside")

        await run_in_thread(_echo, "after")

        assert [hold.label for hold in tracker.holds] == [
            "full table scan",
            f"{THREAD_PREFIX}_echo",
        ]

    @pytest.mark.asyncio
    async def test_a_task_that_outlives_the_block_is_vouched_for_again(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """The detached-task escape: standing down must never leave *nothing*.

        ``asyncio.create_task`` copies the creating context (PEP 567), so a task
        spawned inside the block carries the suppression mark into a lifetime the
        parent's ``reset`` cannot reach. If the mark were a plain boolean, such a
        task offloading *after* the block exits would stand down with no declared
        hold left to cover it — no vouch at all while the watchdog is live, which
        is a worse hole than the one the suppression closes, and precisely the
        false-kill this whole issue exists to prevent.

        Naming the hold instead of flagging it makes the mark falsifiable, so the
        detached task is auto-held again like any other caller.
        """
        block_exited = threading.Event()
        vouched_during_offload: list[bool] = []

        async def _detached() -> None:
            await asyncio.to_thread(block_exited.wait, 5)
            await run_in_thread(lambda: vouched_during_offload.append(tracker.held()))

        async with holding_progress("full table scan", timeout=7200):
            child = asyncio.create_task(_detached())
            await asyncio.sleep(0)  # let it start and park

        block_exited.set()
        await child

        assert vouched_during_offload == [
            True
        ], "an offload outliving the declared block must get its own auto-hold"
        assert [hold.label for hold in tracker.holds] == [
            "full table scan",
            f"{THREAD_PREFIX}<lambda>",
        ]

    @pytest.mark.asyncio
    async def test_a_hold_on_another_tracker_suppresses_nothing(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """A declared hold can only govern the attempt it was declared on.

        If the tracker is rebound between the declaration and the offload, the
        declared hold belongs to the previous attempt — it vouches for nothing
        here, so suppressing on it would leave this attempt's offload unvouched.
        """
        other = RecordingProgressTracker()

        async with holding_progress("full table scan", timeout=7200):
            with bind_progress_tracker(other):
                await run_in_thread(_echo, "different attempt")

        assert [hold.label for hold in other.holds] == [f"{THREAD_PREFIX}_echo"]
        assert [hold.label for hold in tracker.holds] == ["full table scan"]

    @pytest.mark.asyncio
    async def test_a_lapsed_declared_hold_still_suppresses(self) -> None:
        """Suppression tracks the token being *open*, not the hold still vouching.

        Once a declared allowance lapses the watchdog is meant to resume and fire
        one budget later. If the auto-hold re-armed at that moment it would vouch
        unboundedly again and defeat the allowance a second time — so an offload
        started after the lapse, while the block is still open, must stay
        suppressed.
        """
        clock = _FakeClock()
        recording = RecordingProgressTracker(clock=clock)

        with bind_progress_tracker(recording):
            async with holding_progress("full table scan", timeout=600):
                clock.advance(900)  # the declared allowance lapses

                assert recording.held() is False, "the lapsed hold stops vouching"
                await run_in_thread(_echo, "after the lapse")

                # No auto-hold was added, so the watchdog stays free to fire.
                assert recording.held() is False

        assert [hold.label for hold in recording.holds] == ["full table scan"]

    @pytest.mark.asyncio
    async def test_nested_declared_blocks_restore_one_level_at_a_time(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """An inner block exiting must not re-arm the auto-hold under an outer one.

        The mark is a ContextVar token, so leaving the inner block restores the
        outer block's value rather than clearing it — otherwise an offload
        between the inner and outer exits would add exactly the hold this
        precedence rule exists to suppress.
        """
        async with holding_progress("outer", timeout=7200):
            async with holding_progress("inner", timeout=600):
                await run_in_thread(_echo, "innermost")

            await run_in_thread(_echo, "between")

        assert [hold.label for hold in tracker.holds] == ["inner", "outer"]


# ---------------------------------------------------------------------------
# Labels name the site, and only ever code
# ---------------------------------------------------------------------------


class TestTheHoldLabel:
    @pytest.mark.asyncio
    async def test_it_names_the_offloaded_callable(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """So warn mode ranks sites instead of one ``run_in_thread`` bucket."""
        await run_in_thread(_Cursor().execute, "SELECT 1")

        assert tracker.holds[0].label == f"{THREAD_PREFIX}_Cursor.execute"

    @pytest.mark.asyncio
    async def test_no_argument_reaches_the_label(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """Labels are carried into logs and metrics, so this is a data-leak pin.

        The arguments here are the three shapes that would matter if one ever
        leaked: a query with a tenant-shaped literal, a filesystem path, and a
        credential-shaped value.
        """
        await run_in_thread(
            _Cursor().execute,
            "SELECT * FROM example_tenant.orders WHERE region = 'apac'",
        )
        await run_in_thread(_echo, "/local/tmp/artifacts/run-1/chunk-0.parquet")
        await run_in_thread(_echo, "not-a-real-secret-value")

        labels = " ".join(hold.label for hold in tracker.holds)
        for leak in ("example_tenant", "SELECT", "artifacts", "secret", "apac"):
            assert leak not in labels

    @pytest.mark.asyncio
    async def test_a_partial_is_named_by_the_callable_it_wraps(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """``functools.partial`` carries no name of its own; the wrapped one does."""
        import functools

        await run_in_thread(functools.partial(functools.partial(_echo), "value"))

        assert tracker.holds[0].label == f"{THREAD_PREFIX}_echo"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("func", "expected"),
        [
            (lambda: "lambda", "<lambda>"),
            (_Callable(), "_Callable"),
            (MagicMock(return_value=None), "MagicMock"),
        ],
        ids=["lambda", "callable-instance", "mock"],
    )
    async def test_a_nameless_callable_still_gets_a_usable_label(
        self, tracker: RecordingProgressTracker, func: Any, expected: str
    ) -> None:
        """Every fallback in the naming helper, including the one that matters.

        A ``Mock`` fabricates a child mock for any attribute asked of it, so
        reading ``__qualname__`` without checking it is a string would put a
        ``<MagicMock id=...>`` repr — a per-instance value, so unbounded metric
        cardinality — straight into a label.

        Matched on the tail, not for equality: a lambda or a nested function
        carries its enclosing scope in ``__qualname__`` (``...<locals>.<lambda>``),
        which is verbose but still exactly the identifier that locates the site.
        """
        await run_in_thread(func)

        label = tracker.holds[0].label
        assert label.startswith(THREAD_PREFIX)
        assert label.endswith(expected)
        assert "id=" not in label, "a repr, not an identifier, reached the label"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "hostile_qualname",
        [
            "extract for example_tenant",
            "query('SELECT * FROM orders')",
            "/local/tmp/artifacts/run-1/chunk-0.parquet",
            "fetch\nrows",
            "scan_" + "x" * 200,
        ],
        ids=["spaces", "quoted-value", "path", "newline", "too-long"],
    )
    async def test_a_name_that_is_not_an_identifier_never_reaches_the_label(
        self, tracker: RecordingProgressTracker, hostile_qualname: str
    ) -> None:
        """``__qualname__`` is writable, so "it comes from code" needs enforcing.

        A decorator, a factory or a dynamically-built class can set it to
        anything, including a per-call value. Everywhere else that is harmless;
        here the string lands in the ``last_label`` metric attribute, where a
        per-call value is unbounded series cardinality and a 200-character one is
        dead weight on every stall report.

        Both name attributes are poisoned, so the label falls through to the
        callable's *type* name — still a real identifier, and still useful.
        """

        def _named() -> str:
            return "done"

        _named.__qualname__ = hostile_qualname
        _named.__name__ = hostile_qualname

        await run_in_thread(_named)

        label = tracker.holds[0].label
        assert hostile_qualname not in label
        assert label == f"{THREAD_PREFIX}function"

    @pytest.mark.asyncio
    async def test_an_unnameable_callable_collapses_to_one_constant(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """The bottom rung: every name a callable has is unusable.

        Reached only by a callable whose *class* was also given a junk name, so
        it is a backstop rather than a path real code takes. It matters that the
        backstop is a single constant: one refused label must add one metric
        series, not one per distinct junk value — otherwise refusing the name
        would cost exactly what keeping it did.
        """

        class _Hostile:
            def __call__(self) -> str:
                return "done"

        _Hostile.__qualname__ = "callable for example_tenant"
        _Hostile.__name__ = "callable for example_tenant"

        await run_in_thread(_Hostile())

        assert tracker.holds[0].label == f"{THREAD_PREFIX}{UNNAMEABLE}"

    @pytest.mark.asyncio
    async def test_a_hostile_qualname_falls_back_to_the_plain_name(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """Degrade one rung at a time, not straight to the constant.

        ``__qualname__`` and ``__name__`` are set independently, so a callable
        can carry an unusable one and a perfectly good other. Naming the site is
        the whole point of the label, so a real identifier beats the stable
        constant whenever one is available.
        """

        def _named() -> str:
            return "done"

        _named.__qualname__ = "extract for example_tenant"

        await run_in_thread(_named)

        assert tracker.holds[0].label == f"{THREAD_PREFIX}_named"

    @pytest.mark.asyncio
    async def test_a_deeply_nested_qualname_is_still_kept(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """The length cap must not refuse names real code produces.

        A lambda nested inside a test method already carries its whole enclosing
        scope, which is the longest shape the SDK's own suite generates — if the
        cap cannot admit that, it is set too low to be useful.
        """

        def _outer() -> Callable[[], str]:
            def _deeply_nested_inner_helper_with_a_long_name() -> str:
                return "done"

            return _deeply_nested_inner_helper_with_a_long_name

        await run_in_thread(_outer())

        label = tracker.holds[0].label
        assert label.endswith("_deeply_nested_inner_helper_with_a_long_name")
        assert UNNAMEABLE not in label


# ---------------------------------------------------------------------------
# Outside an activity: byte-for-byte the old behaviour
# ---------------------------------------------------------------------------


class TestWithNoTrackerBound:
    @pytest.mark.asyncio
    async def test_the_offload_is_unchanged_and_silent(self, loguru_capture) -> None:
        """Local runs, unit tests, scripts: inert, and no warning about it.

        The inert tracker hands back a token no real tracker can own, and its
        ``exit_hold`` stays quiet — so the release path must not log the
        "no such hold" warning on every offload made outside an activity.
        """
        assert await run_in_thread(_echo, "hi") == "echo:hi"

        assert current_progress_tracker().held() is False
        assert current_progress_tracker().last_label == ""
        assert not [
            record
            for record in loguru_capture
            if "exit_hold" in record["message"] or "no such hold" in record["message"]
        ]


# ---------------------------------------------------------------------------
# run_fault_isolated — the other offload seam, bounded by its own timeout
# ---------------------------------------------------------------------------


class TestTheIsolatedOffloadHold:
    @pytest.mark.asyncio
    async def test_a_declared_timeout_becomes_the_allowance(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """Not a derived bound: this ``timeout`` is enforced here as a kill.

        What ADR-0018 rejects is inferring an allowance from the *callee's*
        per-operation kwargs. ``run_fault_isolated``'s own ``timeout`` is a
        wall-clock ceiling it enforces by killing the child, which is exactly
        what an allowance means — so reusing it invents nothing.
        """
        assert await run_fault_isolated(_echo, "hi", timeout=30) == "echo:hi"

        assert len(tracker.holds) == 1
        closed = tracker.holds[0]
        assert closed.label == f"{ISOLATED_PREFIX}_echo"
        assert closed.allowance_seconds == 30
        assert closed.lapsed is False

    @pytest.mark.asyncio
    async def test_no_timeout_means_an_unbounded_hold(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """``timeout=None`` waits forever, so the hold vouches without a bound."""
        await run_fault_isolated(_echo, "hi")

        assert tracker.holds[0].allowance_seconds is None

    @pytest.mark.asyncio
    async def test_released_when_the_timeout_kills_the_child(
        self, tracker: RecordingProgressTracker
    ) -> None:
        with pytest.raises(TimeoutError):
            await run_fault_isolated(_sleep_forever, timeout=0.5)

        assert tracker.held() is False
        assert len(tracker.holds) == 1

    @pytest.mark.asyncio
    async def test_released_when_the_child_raises(
        self, tracker: RecordingProgressTracker
    ) -> None:
        with pytest.raises(ValueError, match="boom"):
            await run_fault_isolated(_raise_value_error)

        assert tracker.held() is False
        assert len(tracker.holds) == 1

    @pytest.mark.asyncio
    async def test_a_rejected_pool_width_records_no_hold(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """The argument check runs before the hold, so nothing was offloaded.

        Entering first would report a zero-duration hold for a call that never
        reached a child — noise in the audit for a programming error.
        """
        with pytest.raises(ValueError):
            await run_fault_isolated(_echo, "hi", max_workers=0)

        assert tracker.holds == []
        assert tracker.held() is False

    @pytest.mark.asyncio
    async def test_best_effort_delegates_one_hold(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """The policy layer holds through the mechanism, not on top of it."""
        logger = MagicMock()

        assert await run_best_effort(_echo, "hi", label="Echo", logger=logger) == (
            "echo:hi"
        )

        assert len(tracker.holds) == 1
        assert tracker.holds[0].label == f"{ISOLATED_PREFIX}_echo"

    @pytest.mark.asyncio
    async def test_a_foreign_pool_discard_still_releases(
        self, tracker: RecordingProgressTracker
    ) -> None:
        """Both concurrent callers release, including the one killed by the other.

        One caller's timeout discards the shared pool and breaks the other's
        in-flight child. Two offloads went out, so two holds must come back —
        the abandoned one is exactly the token that would leak.
        """
        hung, foreign = await asyncio.gather(
            run_fault_isolated(_sleep_forever, timeout=0.5),
            run_fault_isolated(_sleep_forever, timeout=30),
            return_exceptions=True,
        )

        assert isinstance(hung, TimeoutError)
        assert isinstance(foreign, BrokenProcessPool)
        assert len(tracker.holds) == 2
        assert tracker.held() is False
