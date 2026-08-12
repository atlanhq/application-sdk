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
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import cast
from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.context import AppContext, TaskExecutionContext
from application_sdk.app.registry import TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution import progress as progress_module
from application_sdk.execution._temporal import activities as activities_module
from application_sdk.execution._temporal.activities import (
    TaskContext,
    create_activity_from_task,
)
from application_sdk.execution.heartbeat import NoopHeartbeatController, run_in_thread
from application_sdk.execution.progress import (
    ProgressTracker,
    current_progress_tracker,
    reset_progress_tracker,
    set_progress_tracker,
)


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


class TestBinding:
    def test_bound_tracker_is_returned(self) -> None:
        tracker = ProgressTracker()
        token = set_progress_tracker(tracker)
        try:
            assert current_progress_tracker() is tracker
        finally:
            reset_progress_tracker(token)

    def test_reset_restores_the_previous_binding(self) -> None:
        outer = ProgressTracker()
        inner = ProgressTracker()
        outer_token = set_progress_tracker(outer)
        try:
            inner_token = set_progress_tracker(inner)
            assert current_progress_tracker() is inner
            reset_progress_tracker(inner_token)
            assert current_progress_tracker() is outer
        finally:
            reset_progress_tracker(outer_token)

    def test_reset_restores_the_inert_tracker(self) -> None:
        tracker = ProgressTracker()
        reset_progress_tracker(set_progress_tracker(tracker))

        assert current_progress_tracker() is not tracker
        assert current_progress_tracker().stalled_for() == 0.0


class TestReachableFromRunInThread:
    """Both entry points must reach the tracker, not only the wrapper."""

    @pytest.mark.asyncio
    async def test_module_level_run_in_thread(self) -> None:
        tracker = ProgressTracker()
        token = set_progress_tracker(tracker)
        try:
            seen = await run_in_thread(_read_tracker_deep)
        finally:
            reset_progress_tracker(token)

        assert seen is tracker
        # The context is copied into the thread, but the tracker object is
        # shared — so progress marked in the thread lands on the attempt's
        # tracker rather than on a private copy that dies with the thread.
        assert tracker.last_label == "from_thread"

    @pytest.mark.asyncio
    async def test_task_execution_context_run_in_thread(self) -> None:
        tracker = ProgressTracker()
        token = set_progress_tracker(tracker)
        try:
            seen = await _task_execution_context().run_in_thread(_read_tracker_deep)
        finally:
            reset_progress_tracker(token)

        assert seen is tracker
        assert tracker.last_label == "from_thread"

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

        token = set_progress_tracker(tracker)
        try:
            assert await _opaque_call() is tracker
        finally:
            reset_progress_tracker(token)

    @pytest.mark.asyncio
    async def test_rebinding_inside_the_thread_does_not_leak_back(self) -> None:
        """Copy semantics: a rebind inside the thread stays in the thread."""
        outer = ProgressTracker()
        thread_local = ProgressTracker()

        def _rebind() -> ProgressTracker:
            set_progress_tracker(thread_local)
            return current_progress_tracker()

        token = set_progress_tracker(outer)
        try:
            assert await run_in_thread(_rebind) is thread_local
            assert current_progress_tracker() is outer
        finally:
            reset_progress_tracker(token)


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
        ) -> None:
            await stop_event.wait()

        with mock.patch(
            "application_sdk.execution.heartbeat.auto_heartbeat_loop", new=fake_loop
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
        assert from_body.last_label == "from_thread"

    @pytest.mark.asyncio
    async def test_tracker_is_unbound_after_the_activity_returns(self) -> None:
        class _CleanApp(App):
            @task(timeout_seconds=60)
            async def greet(self, input: _ProgIn) -> _ProgOut:
                return _ProgOut(greeting="ok")

            async def run(self, input: _ProgIn) -> _ProgOut:
                return await self.greet(input)

        activity_fn = _activity_for("_clean-app", "greet")
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
