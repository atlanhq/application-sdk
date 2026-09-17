"""Unit tests for the ProgressTracker ContextVar plumbing (FND-287).

Three things are being proven here, because each is a way this plumbing could be
half-right and still look fine:

1. The current attempt's tracker is reachable from the places that actually
   report progress — including from inside a worker thread through *both*
   ``run_in_thread`` entry points (the module-level function and the
   ``TaskExecutionContext`` wrapper), since both are used across the fleet and
   covering only the wrapper would leave the auto-hold silently absent from a
   large share of real blocking calls.
2. Concurrent activities in one worker bind their own tracker and never observe
   each other's.
3. Outside an activity the accessor is inert rather than absent: local runs,
   unit tests and non-activity callers behave exactly as they did before this
   plumbing existed.

The binding itself is a block, so "bound but never unbound" is not a state a
call site can reach — but the block still has to unbind on every path, including
off the event loop and out of a raise, and that is pinned here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import cast
from unittest import mock

import pytest

from application_sdk._runtime import progress as progress_module
from application_sdk.app.base import App
from application_sdk.app.context import AppContext, TaskExecutionContext
from application_sdk.app.registry import TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal import activities as activities_module
from application_sdk.execution._temporal.activities import (
    TaskContext,
    create_activity_from_task,
)
from application_sdk.execution.heartbeat import NoopHeartbeatController, run_in_thread
from application_sdk.execution.progress import (
    ProgressTracker,
    bind_progress_tracker,
    current_progress_tracker,
)
from tests.unit.conftest import RecordingProgressTracker


class _ProgIn(Input, allow_unbounded_fields=True):
    name: str = "default"


class _ProgOut(Output, allow_unbounded_fields=True):
    greeting: str = ""


ActivityFn = Callable[[TaskContext, _ProgIn], Awaitable[_ProgOut]]


def _activity_for(app_name: str, task_name: str) -> ActivityFn:
    tasks = TaskRegistry.get_instance().get_tasks_for_app(app_name)
    activity_fn = create_activity_from_task(
        next(t for t in tasks if t.name == task_name)
    )
    return cast("ActivityFn", activity_fn)


def _task_context(app_name: str, task_name: str, *, heartbeating: bool) -> TaskContext:
    return TaskContext(
        app_name=app_name,
        task_name=task_name,
        run_id="run-1",
        heartbeat_timeout_seconds=60 if heartbeating else None,
        auto_heartbeat_seconds=10 if heartbeating else None,
    )


def _register_trivial_app() -> ActivityFn:
    """Register an app whose task does nothing, and return its activity.

    For the tests that care only about what happens *around* the task body —
    the binding's lifetime on each exit path.
    """

    class _CleanApp(App):
        @task(timeout_seconds=60)
        async def greet(self, input: _ProgIn) -> _ProgOut:
            return _ProgOut(greeting="ok")

        async def run(self, input: _ProgIn) -> _ProgOut:
            return await self.greet(input)

    return _activity_for("_clean-app", "greet")


def _task_execution_context() -> TaskExecutionContext:
    """A ``TaskExecutionContext`` with no Temporal behind it."""
    return TaskExecutionContext(
        app_context=AppContext(app_name="_prog-app", app_version="0.0.0"),
        task_name="greet",
        heartbeat_controller=NoopHeartbeatController(),
    )


def _read_tracker_deep() -> ProgressTracker:
    """Read the tracker from a nested blocking frame, not from the entry frame.

    The producers this plumbing serves (a transfer loop, an app's own blocking
    helper) are never the function handed to ``run_in_thread`` — they sit
    several frames below it.
    """

    def _inner() -> ProgressTracker:
        tracker = current_progress_tracker()
        tracker.mark_progress("from_thread")
        return tracker

    return _inner()


class TestNoTrackerBound:
    """Outside an activity every call is inert — never ``None``, never a raise."""

    def test_accessor_returns_an_inert_tracker(self) -> None:
        tracker = current_progress_tracker()

        assert isinstance(tracker, ProgressTracker)
        assert tracker.held() is False
        assert tracker.stalled_for() == 0.0
        assert tracker.last_label == ""

    def test_progress_and_holds_are_no_ops(self) -> None:
        tracker = current_progress_tracker()

        tracker.mark_progress("write_batch")
        token = tracker.enter_hold("full table scan", timeout=None)
        tracker.exit_hold(token)

        # Nothing recorded, and nothing accumulated on a process-wide singleton
        # that would otherwise grow one hold per never-exited token.
        assert tracker.last_label == ""
        assert tracker.stalled_for() == 0.0
        assert tracker.held() is False

    def test_inert_token_cannot_release_a_real_tracker_hold(self) -> None:
        """An inert token is negative, so a real tracker rejects it.

        This is the failure mode of a consumer that reads the tracker twice
        instead of holding on to it: enter outside the attempt, exit inside.
        Releasing an unrelated real hold would silently make stall accounting
        lenient; a warning does not.
        """
        inert_token = current_progress_tracker().enter_hold("opaque", timeout=None)
        real = ProgressTracker()
        real.enter_hold("snapshot metadata query", timeout=None)

        real.exit_hold(inert_token)

        assert real.held() is True

    def test_exit_hold_stays_quiet(self) -> None:
        """No hold existed, so an unpaired exit is not broken plumbing."""
        with mock.patch.object(progress_module.logger, "warning") as warning:
            current_progress_tracker().exit_hold(-1)

        assert warning.call_count == 0

    def test_a_stall_verdict_cannot_stick_to_the_inert_singleton(self) -> None:
        """The one no-op whose absence would leak across every later attempt.

        The inert tracker is process-wide, so a verdict recorded on it would
        outlive its caller and make every subsequent cancellation anywhere in the
        process — a real worker eviction included — read as a stall kill.
        """
        current_progress_tracker().flag_stalled(
            stalled_for_seconds=900.0, last_progress_label="writer.flush_buffer"
        )

        assert current_progress_tracker().stall is None


class TestBinding:
    def test_the_block_yields_and_binds_the_same_tracker(self) -> None:
        tracker = ProgressTracker()

        with bind_progress_tracker(tracker) as bound:
            assert bound is tracker
            assert current_progress_tracker() is tracker

    def test_exit_restores_the_previous_binding(self) -> None:
        outer = ProgressTracker()
        inner = ProgressTracker()

        with bind_progress_tracker(outer):
            with bind_progress_tracker(inner):
                assert current_progress_tracker() is inner
            assert current_progress_tracker() is outer

    def test_exit_restores_the_inert_tracker(self) -> None:
        tracker = ProgressTracker()

        with bind_progress_tracker(tracker):
            pass

        assert current_progress_tracker() is not tracker
        assert current_progress_tracker().stalled_for() == 0.0

    def test_a_raising_block_still_unbinds(self) -> None:
        """The property the block exists for: no path leaves a binding behind."""
        tracker = ProgressTracker()

        with pytest.raises(ValueError, match="boom"), bind_progress_tracker(tracker):
            raise ValueError("boom")

        assert current_progress_tracker() is not tracker


class TestReachableFromRunInThread:
    """Both entry points must reach the tracker, not only the wrapper."""

    @pytest.mark.asyncio
    async def test_module_level_run_in_thread(self) -> None:
        # A recording tracker, because the label history is what proves the mark
        # landed: the offload's own auto-hold (FND-290) releases *after* the
        # thread returns and re-arms the stall clock under its own label, so
        # `last_label` is the hold's by the time the await completes.
        tracker = RecordingProgressTracker()

        with bind_progress_tracker(tracker):
            seen = await run_in_thread(_read_tracker_deep)

        assert seen is tracker
        # The context is copied into the thread, but the tracker object is
        # shared — so progress marked in the thread lands on the attempt's
        # tracker rather than on a private copy that dies with the thread.
        assert "from_thread" in tracker.labels

    @pytest.mark.asyncio
    async def test_task_execution_context_run_in_thread(self) -> None:
        tracker = RecordingProgressTracker()

        with bind_progress_tracker(tracker):
            seen = await _task_execution_context().run_in_thread(_read_tracker_deep)

        assert seen is tracker
        assert "from_thread" in tracker.labels

    @pytest.mark.asyncio
    async def test_reachable_from_a_nested_async_frame(self) -> None:
        """The seam FND-291's ``holding_progress()`` will read.

        It is entered from app code an arbitrary depth below the task method,
        with no tracker in hand.
        """
        tracker = ProgressTracker()

        async def _opaque_call() -> ProgressTracker:
            await asyncio.sleep(0)
            return current_progress_tracker()

        with bind_progress_tracker(tracker):
            assert await _opaque_call() is tracker

    @pytest.mark.asyncio
    async def test_binding_inside_the_thread_is_confined_to_it(self) -> None:
        """A nested bind off the event loop restores, and never reaches back."""
        outer = ProgressTracker()
        thread_local = ProgressTracker()

        def _rebind() -> tuple[ProgressTracker, ProgressTracker]:
            with bind_progress_tracker(thread_local):
                inside = current_progress_tracker()
            return inside, current_progress_tracker()

        with bind_progress_tracker(outer):
            inside, after_exit = await run_in_thread(_rebind)

            assert inside is thread_local
            # Restored within the thread's copy of the context...
            assert after_exit is outer
            # ...and the caller's binding was never touched.
            assert current_progress_tracker() is outer


class TestActivityBinding:
    """The activity body owns one tracker per attempt and unbinds it after."""

    @pytest.fixture(autouse=True)
    def _no_temporal(self) -> Iterator[None]:
        """Run the real activity body with no worker and no infrastructure."""
        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-prog"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
        ):
            yield

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "heartbeating", [False, True], ids=["heartbeating-off", "heartbeating-on"]
    )
    async def test_task_body_sees_its_attempt_tracker(self, heartbeating: bool) -> None:
        """A task with heartbeating disabled still gets a tracker.

        The stall watchdog is what bounds a wedged attempt on such a task, so
        the binding must not be gated on the heartbeat settings.
        """
        seen: list[ProgressTracker] = []

        class _SeeApp(App):
            @task(timeout_seconds=60)
            async def greet(self, input: _ProgIn) -> _ProgOut:
                tracker = current_progress_tracker()
                tracker.mark_progress("greeting")
                seen.append(tracker)
                return _ProgOut(greeting=f"hi {input.name}")

            async def run(self, input: _ProgIn) -> _ProgOut:
                return await self.greet(input)

        activity_fn = _activity_for("_see-app", "greet")

        async def fake_loop(
            *,
            interval_seconds: float,
            heartbeat_fn: Callable[[], None],
            stop_event: asyncio.Event,
            task_name: str,
            **_watchdog: object,
        ) -> None:
            await stop_event.wait()

        with mock.patch(
            "application_sdk.execution.heartbeat.auto_heartbeat_loop",
            new=fake_loop,
        ):
            result = await activity_fn(
                _task_context("_see-app", "greet", heartbeating=heartbeating),
                _ProgIn(name="vee"),
            )

        assert result.greeting == "hi vee"
        # A real tracker, not the inert one: it recorded what the task reported.
        assert [t.last_label for t in seen] == ["greeting"]

    @pytest.mark.asyncio
    async def test_run_in_thread_inside_the_activity_reaches_the_tracker(self) -> None:
        """The composed shape the fleet actually runs: offload inside a task."""
        seen: list[ProgressTracker] = []

        class _OffloadApp(App):
            @task(timeout_seconds=60)
            async def scan(self, input: _ProgIn) -> _ProgOut:
                seen.append(current_progress_tracker())
                seen.append(await self.task_context.run_in_thread(_read_tracker_deep))
                seen.append(await run_in_thread(_read_tracker_deep))
                return _ProgOut(greeting="scanned")

            async def run(self, input: _ProgIn) -> _ProgOut:
                return await self.scan(input)

        activity_fn = _activity_for("_offload-app", "scan")

        await activity_fn(
            _task_context("_offload-app", "scan", heartbeating=False),
            _ProgIn(name="x"),
        )

        from_body, from_wrapper, from_module_level = seen
        assert from_wrapper is from_body
        assert from_module_level is from_body
        # The activity owns this tracker, so there is no label history to read —
        # only the most recent signal, which is the second offload's auto-hold
        # (FND-290) releasing under the offloaded callable's name. That is the
        # composed shape end to end: bound by `activities.py`, reached through
        # both entry points, and vouched for on the way back out.
        assert from_body.last_label == "run_in_thread._read_tracker_deep"

    @pytest.mark.asyncio
    async def test_tracker_is_unbound_after_the_activity_returns(self) -> None:
        activity_fn = _register_trivial_app()
        before = current_progress_tracker()

        await activity_fn(
            _task_context("_clean-app", "greet", heartbeating=False), _ProgIn(name="x")
        )

        assert current_progress_tracker() is before

    @pytest.mark.asyncio
    async def test_tracker_is_unbound_when_the_task_raises(self) -> None:
        class _BoomApp(App):
            @task(timeout_seconds=60)
            async def boom(self, input: _ProgIn) -> _ProgOut:
                raise ValueError("ordinary failure")

            async def run(self, input: _ProgIn) -> _ProgOut:
                return await self.boom(input)

        activity_fn = _activity_for("_boom-app", "boom")
        before = current_progress_tracker()

        with pytest.raises(ValueError, match="ordinary failure"):
            await activity_fn(
                _task_context("_boom-app", "boom", heartbeating=False),
                _ProgIn(name="x"),
            )

        assert current_progress_tracker() is before

    @pytest.mark.asyncio
    async def test_tracker_is_unbound_when_heartbeat_setup_raises(self) -> None:
        """A raise before the body starts must not leak the binding.

        `asyncio.create_task` fails when the loop is closing — a worker being
        torn down mid-dispatch. The dead attempt's tracker would otherwise stay
        bound to the context the worker keeps using.
        """
        activity_fn = _register_trivial_app()
        before = current_progress_tracker()

        with (
            mock.patch.object(
                activities_module.asyncio,
                "create_task",
                side_effect=RuntimeError("event loop is closed"),
            ),
            pytest.raises(RuntimeError, match="event loop is closed"),
        ):
            await activity_fn(
                _task_context("_clean-app", "greet", heartbeating=True),
                _ProgIn(name="x"),
            )

        assert current_progress_tracker() is before

    @pytest.mark.asyncio
    async def test_tracker_is_unbound_when_cancelled_during_cleanup(self) -> None:
        """`CancelledError` in the cleanup path must not skip the unbind.

        It is a `BaseException`, so only ``stop_heartbeat_task``'s explicit
        ``BaseException`` guards contain it — the cleanup lets nothing escape,
        the unbind sequenced after it still runs, and the activity's result
        survives its own heartbeat task dying.
        """
        activity_fn = _register_trivial_app()
        before = current_progress_tracker()

        async def cancelling_loop(
            *,
            interval_seconds: float,
            heartbeat_fn: Callable[[], None],
            stop_event: asyncio.Event,
            task_name: str,
            **_watchdog: object,
        ) -> None:
            await stop_event.wait()
            raise asyncio.CancelledError

        with mock.patch(
            "application_sdk.execution.heartbeat.auto_heartbeat_loop",
            new=cancelling_loop,
        ):
            result = await activity_fn(
                _task_context("_clean-app", "greet", heartbeating=True),
                _ProgIn(name="x"),
            )

        assert result == _ProgOut(greeting="ok")
        assert current_progress_tracker() is before

    @pytest.mark.asyncio
    async def test_concurrent_activities_do_not_share_a_tracker(self) -> None:
        """Two attempts in one worker, both inside the body at the same time."""
        both_inside = asyncio.Barrier(2)
        seen: dict[str, ProgressTracker] = {}

        class _ConcurrentApp(App):
            @task(timeout_seconds=60)
            async def greet(self, input: _ProgIn) -> _ProgOut:
                current_progress_tracker().mark_progress(input.name)
                # Hold both attempts inside the body simultaneously, so a shared
                # binding is observable rather than merely sequential.
                await both_inside.wait()
                seen[input.name] = current_progress_tracker()
                return _ProgOut(greeting=input.name)

            async def run(self, input: _ProgIn) -> _ProgOut:
                return await self.greet(input)

        activity_fn = _activity_for("_concurrent-app", "greet")
        ctx = _task_context("_concurrent-app", "greet", heartbeating=False)

        # Each attempt runs as its own asyncio task, exactly as Temporal's
        # worker dispatches them — awaiting bare coroutines would run them in
        # this test's own context and prove nothing.
        await asyncio.gather(
            asyncio.create_task(activity_fn(ctx, _ProgIn(name="alpha"))),
            asyncio.create_task(activity_fn(ctx, _ProgIn(name="beta"))),
        )

        assert seen["alpha"] is not seen["beta"]
        assert seen["alpha"].last_label == "alpha"
        assert seen["beta"].last_label == "beta"
