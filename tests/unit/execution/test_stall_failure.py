"""How a stall becomes a typed, diagnosable failure (FND-289, ADR-0018).

Three things are proven here, because each is a way this path could look wired
and still be wrong in production:

1. **The attribution.** A stall kill and a worker eviction arrive at the same
   ``except asyncio.CancelledError`` handler as the same exception type. Only the
   tracker's verdict separates them, and the failure a human reads afterwards
   has to name the gap and the last progress signal — a bare ``CancelledError``
   names neither.
2. **The order of the two checks.** A stall and a SIGTERM can coincide. If
   eviction won that race the attempt would be re-dispatched by the
   workflow-side eviction-retry loop *outside* the normal retry budget, so the
   matrix of (stall, shutting down) is pinned in all four corners.
3. **What is wired, and what deliberately is not.** The handler is injected now;
   the mode and the budget that make the watchdog act are FND-296's, so on this
   branch the watchdog is inert and the behaviour change is zero. A test asserts
   that absence rather than trusting it.

The end-to-end test drives the *real* watchdog inside the *real* activity body,
with only the enforce config injected — no patched clock (an asyncio loop shares
``time.monotonic``), just a 10 ms tick against a 50 ms budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.errors.leaves import WORKER_EVICTED_TYPE, TaskStalledError
from application_sdk.errors.wire import FailureDetails
from application_sdk.execution import shutdown as shutdown_module
from application_sdk.execution._temporal import activities as activities_module
from application_sdk.execution._temporal.activities import (
    TaskContext,
    create_activity_from_task,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.execution.heartbeat import (
    auto_heartbeat_loop as _real_auto_heartbeat_loop,
)
from application_sdk.execution.progress import (
    ProgressTracker,
    ProgressWatchdogMode,
    current_progress_tracker,
)

_HEARTBEAT_LOOP = "application_sdk.execution.heartbeat.auto_heartbeat_loop"


class _StallIn(Input, allow_unbounded_fields=True):
    name: str = "x"


class _StallOut(Output, allow_unbounded_fields=True):
    msg: str = ""


ActivityFn = Callable[[TaskContext, _StallIn], Awaitable[_StallOut]]


def _activity_for(app_name: str, task_name: str) -> ActivityFn:
    tasks = TaskRegistry.get_instance().get_tasks_for_app(app_name)
    return cast(
        "ActivityFn",
        create_activity_from_task(next(t for t in tasks if t.name == task_name)),
    )


def _task_context(
    app_name: str, task_name: str, *, heartbeating: bool = True
) -> TaskContext:
    return TaskContext(
        app_name=app_name,
        task_name=task_name,
        run_id="run-1",
        heartbeat_timeout_seconds=60 if heartbeating else None,
        auto_heartbeat_seconds=10 if heartbeating else None,
    )


def _enforcing_watchdog(
    *, budget_seconds: float = 0.05, tick_seconds: float = 0.01
) -> Callable[..., Awaitable[None]]:
    """The real auto-heartbeat loop, with the config FND-296 will supply.

    Everything under test stays real — the tick, the gap arithmetic, the
    ``on_stall`` call, the loop returning immediately after enforcing. Only
    ``watchdog_mode`` and ``max_no_progress_seconds`` are injected, since
    ``activities.py`` has nothing to read them from yet.
    """

    async def loop(**kwargs: Any) -> None:
        kwargs["interval_seconds"] = tick_seconds
        await _real_auto_heartbeat_loop(
            **kwargs,
            max_no_progress_seconds=budget_seconds,
            watchdog_mode=ProgressWatchdogMode.ENFORCE,
        )

    return loop


def _watchdog_reporting(
    stalled_for: float, last_label: str
) -> Callable[..., Awaitable[None]]:
    """A watchdog stand-in that reports one stall, then returns as the real one does.

    Used where the *values* in the failure matter: the real loop can only produce
    whatever gap wall-clock happens to give it, and a failure message is worth
    asserting exactly.
    """

    async def loop(*, on_stall: Callable[[float, str], None], **_kwargs: Any) -> None:
        on_stall(stalled_for, last_label)

    return loop


async def _idle_watchdog(*, stop_event: asyncio.Event, **_kwargs: Any) -> None:
    """A watchdog stand-in that observes nothing — no stall, ever."""
    await stop_event.wait()


def _quiet_activity() -> ActivityFn:
    """An attempt that goes quiet: no progress signal, and it never returns.

    Defined per call rather than at module scope because registration happens at
    class-definition time and the registries are reset between tests.
    """

    class _QuietApp(App):
        @task(timeout_seconds=600)
        async def quiet(self, input: _StallIn) -> _StallOut:
            await asyncio.sleep(3600)
            return _StallOut(msg="never")

        async def run(self, input: _StallIn) -> _StallOut:
            return await self.quiet(input)

    return _activity_for("_quiet-app", "quiet")


def _self_cancelling_activity() -> ActivityFn:
    """An attempt cancelled by something that is neither the watchdog nor a SIGTERM."""

    class _SelfCancellingApp(App):
        @task(timeout_seconds=600)
        async def boom(self, input: _StallIn) -> _StallOut:
            raise asyncio.CancelledError

        async def run(self, input: _StallIn) -> _StallOut:
            return await self.boom(input)

    return _activity_for("_self-cancelling-app", "boom")


@pytest.fixture(autouse=True)
def _clean_worker_state() -> Any:
    AppRegistry.reset()
    TaskRegistry.reset()
    shutdown_module.reset_worker_shutting_down()
    yield
    AppRegistry.reset()
    TaskRegistry.reset()
    shutdown_module.reset_worker_shutting_down()


@pytest.fixture(autouse=True)
def _no_infrastructure() -> Any:
    with (
        mock.patch.object(
            activities_module.activity,
            "info",
            return_value=mock.MagicMock(workflow_id="wf-stall"),
        ),
        mock.patch(
            "application_sdk.infrastructure.context.get_infrastructure",
            return_value=None,
        ),
    ):
        yield


def _details_of(error: ApplicationError) -> FailureDetails:
    assert error.details, "the failure carried no structured details"
    details = error.details[0]
    assert isinstance(details, FailureDetails)
    return details


class TestStallKill:
    async def test_a_stalled_attempt_fails_with_task_stalled_error(self) -> None:
        """End to end: real watchdog, real activity body, real cancellation handler."""
        activity_fn = _quiet_activity()

        with (
            mock.patch(_HEARTBEAT_LOOP, new=_enforcing_watchdog()),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(_task_context("_quiet-app", "quiet"), _StallIn())

        error = exc_info.value
        # The wire type is what Temporal serialises and what a retry policy or a
        # timeout-subtype classifier can key on — the Python class does not cross
        # the activity boundary.
        assert error.type == "TaskStalledError"
        details = _details_of(error)
        assert details.code == "TIMEOUT_TASK_STALLED"
        assert details.evidence["stalled_for_seconds"] >= 0.05
        assert details.evidence["operation"] == "quiet"
        assert details.app_name == "_quiet-app"
        assert details.run_id == "run-1"

    async def test_a_stall_kill_is_retryable_at_the_temporal_layer(self) -> None:
        """The deliberate half of the ADR-0018 argument, at the boundary that acts on it.

        ``non_retryable=True`` here would convert the self-healing majority — a
        transient source hang the app never surfaced — into failed runs needing a
        manual re-run that restarts from zero anyway.
        """
        activity_fn = _quiet_activity()

        with (
            mock.patch(_HEARTBEAT_LOOP, new=_enforcing_watchdog()),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(_task_context("_quiet-app", "quiet"), _StallIn())

        assert exc_info.value.non_retryable is False
        assert _details_of(exc_info.value).retryable is True

    async def test_the_failure_names_the_gap_and_the_last_signal(self) -> None:
        activity_fn = _quiet_activity()

        with (
            mock.patch(
                _HEARTBEAT_LOOP,
                new=_watchdog_reporting(915.0, "writer.flush_buffer"),
            ),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(_task_context("_quiet-app", "quiet"), _StallIn())

        assert exc_info.value.message == (
            "Task 'quiet' made no observable progress for 915s; "
            "last signal was 'writer.flush_buffer'"
        )
        evidence = _details_of(exc_info.value).evidence
        assert evidence["stalled_for_seconds"] == 915.0
        assert evidence["last_progress_label"] == "writer.flush_buffer"

    async def test_an_attempt_that_never_reported_progress_says_so(self) -> None:
        """ "Nothing was ever observed" is a different finding from "it went quiet after X".

        It is the shape that means the task has no instrumentation on its path at
        all, which is the work-list entry an app owner acts on — so the failure
        must not disguise it as an unnamed signal.
        """
        activity_fn = _quiet_activity()

        with (
            mock.patch(_HEARTBEAT_LOOP, new=_watchdog_reporting(60.0, "")),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(_task_context("_quiet-app", "quiet"), _StallIn())

        assert "last signal was '<none>'" in exc_info.value.message
        assert _details_of(exc_info.value).evidence["last_progress_label"] is None

    async def test_the_handler_cancels_the_attempt_not_the_watchdog(self) -> None:
        """The handler runs inside the heartbeat task, so it cannot ask for "this" task.

        Reading ``asyncio.current_task()`` from inside ``on_stall`` would cancel
        the watchdog and leave the wedged attempt running — which fails as a
        never-returning activity rather than as a stall, i.e. exactly the failure
        mode this issue exists to end. If the wrong task were cancelled the
        stand-in below would die before it recorded that it had finished.
        """
        activity_fn = _quiet_activity()
        watchdog_finished: list[bool] = []

        async def loop(
            *, on_stall: Callable[[float, str], None], **_kwargs: Any
        ) -> None:
            on_stall(900.0, "extract.page")
            # The real loop returns straight after enforcing; reaching this line
            # proves the cancellation did not land here.
            watchdog_finished.append(True)

        with (
            mock.patch(_HEARTBEAT_LOOP, new=loop),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(_task_context("_quiet-app", "quiet"), _StallIn())

        assert exc_info.value.type == "TaskStalledError"
        assert watchdog_finished == [True]

    async def test_the_attempt_tracker_is_unbound_after_a_stall_kill(self) -> None:
        """The ``finally`` still runs on the path that swaps the exception type."""
        activity_fn = _quiet_activity()
        before = current_progress_tracker()

        with (
            mock.patch(_HEARTBEAT_LOOP, new=_watchdog_reporting(900.0, "extract.page")),
            pytest.raises(ApplicationError),
        ):
            await activity_fn(_task_context("_quiet-app", "quiet"), _StallIn())

        assert current_progress_tracker() is before

    async def test_an_unbuildable_envelope_still_fails_as_a_stall(self) -> None:
        """The shared translation's guard covers this branch too.

        A secondary failure while building the details must cost the structured
        evidence, not replace the stall with a serialisation error — the same
        contract the ordinary ``AppError`` branch has, which is the point of both
        branches calling one helper.
        """
        activity_fn = _quiet_activity()

        with (
            mock.patch(_HEARTBEAT_LOOP, new=_watchdog_reporting(900.0, "extract.page")),
            mock.patch.object(
                TaskStalledError,
                "to_failure_details",
                side_effect=RuntimeError("evidence not serialisable"),
            ),
            mock.patch.object(activities_module.logger, "warning") as warning,
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(_task_context("_quiet-app", "quiet"), _StallIn())

        assert exc_info.value.type == "TaskStalledError"
        assert exc_info.value.details == ()
        assert warning.call_count == 1


class TestFlagCheckOrder:
    """The (stall, shutting down) matrix, in all four corners."""

    async def test_a_stall_during_shutdown_is_attributed_to_the_stall(self) -> None:
        """Stall wins, so the attempt is not re-dispatched outside its retry budget.

        ``WorkerEvicted`` is non-retryable at the Temporal layer *and* re-run by
        the workflow-side eviction loop, which does not count against the task's
        retry budget. A wedged attempt attributed there would be re-dispatched
        past the point where retries were supposed to stop.
        """
        activity_fn = _quiet_activity()
        shutdown_module.mark_worker_shutting_down()

        with (
            mock.patch(_HEARTBEAT_LOOP, new=_watchdog_reporting(900.0, "extract.page")),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(_task_context("_quiet-app", "quiet"), _StallIn())

        assert exc_info.value.type == "TaskStalledError"
        assert exc_info.value.non_retryable is False

    async def test_shutdown_without_a_stall_is_still_worker_evicted(self) -> None:
        """The new branch must not annex the eviction path it sits in front of."""
        activity_fn = _self_cancelling_activity()
        shutdown_module.mark_worker_shutting_down()

        with (
            mock.patch(_HEARTBEAT_LOOP, new=_idle_watchdog),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(_task_context("_self-cancelling-app", "boom"), _StallIn())

        assert exc_info.value.type == WORKER_EVICTED_TYPE
        assert exc_info.value.non_retryable is True

    async def test_an_unattributed_cancel_still_propagates(self) -> None:
        activity_fn = _self_cancelling_activity()

        with (
            mock.patch(_HEARTBEAT_LOOP, new=_idle_watchdog),
            pytest.raises(asyncio.CancelledError),
        ):
            await activity_fn(_task_context("_self-cancelling-app", "boom"), _StallIn())

    async def test_a_healthy_attempt_is_untouched(self) -> None:
        class _FineApp(App):
            @task(timeout_seconds=600)
            async def fine(self, input: _StallIn) -> _StallOut:
                current_progress_tracker().mark_progress("extract.page")
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.fine(input)

        _FineApp()
        activity_fn = _activity_for("_fine-app", "fine")

        with mock.patch(_HEARTBEAT_LOOP, new=_enforcing_watchdog()):
            result = await activity_fn(_task_context("_fine-app", "fine"), _StallIn())

        assert result.msg == "ok"


class TestWatchdogWiring:
    async def test_the_watchdog_is_handed_the_attempts_tracker_and_a_handler(
        self,
    ) -> None:
        seen: dict[str, Any] = {}
        from_body: list[ProgressTracker] = []

        class _WiredApp(App):
            @task(timeout_seconds=600)
            async def wired(self, input: _StallIn) -> _StallOut:
                from_body.append(current_progress_tracker())
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.wired(input)

        async def loop(*, stop_event: asyncio.Event, **kwargs: Any) -> None:
            seen.update(kwargs)
            await stop_event.wait()

        _WiredApp()
        activity_fn = _activity_for("_wired-app", "wired")

        with mock.patch(_HEARTBEAT_LOOP, new=loop):
            await activity_fn(_task_context("_wired-app", "wired"), _StallIn())

        # The same object the framework hooks report into — a watchdog watching a
        # second tracker would observe silence while work advanced.
        assert seen["progress"] is from_body[0]
        assert callable(seen["on_stall"])

    async def test_the_watchdog_config_is_left_to_the_flag(self) -> None:
        """No mode and no budget, so the watchdog is inert on this branch.

        This is what makes the claim "zero behaviour change" checkable rather
        than argued: the handler is injected, and turning the watchdog on is a
        config change (FND-296), not another seam.
        """
        seen: dict[str, Any] = {}

        class _InertApp(App):
            @task(timeout_seconds=600)
            async def inert(self, input: _StallIn) -> _StallOut:
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.inert(input)

        async def loop(*, stop_event: asyncio.Event, **kwargs: Any) -> None:
            seen.update(kwargs)
            await stop_event.wait()

        _InertApp()
        activity_fn = _activity_for("_inert-app", "inert")

        with mock.patch(_HEARTBEAT_LOOP, new=loop):
            await activity_fn(_task_context("_inert-app", "inert"), _StallIn())

        assert "watchdog_mode" not in seen
        assert "max_no_progress_seconds" not in seen

    async def test_no_heartbeat_loop_means_no_watchdog(self) -> None:
        """A task with heartbeating disabled runs no loop, so nothing can stall it.

        Recorded rather than asserted as desirable: the tracker is bound for such
        a task, but the watchdog lives in the auto-heartbeat loop, so today the
        duration backstop is that task's only bound.
        """
        loop_calls: list[dict[str, Any]] = []

        class _NoBeatApp(App):
            @task(timeout_seconds=600)
            async def nobeat(self, input: _StallIn) -> _StallOut:
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.nobeat(input)

        async def loop(**kwargs: Any) -> None:
            loop_calls.append(kwargs)

        activity_fn = _activity_for("_no-beat-app", "nobeat")

        with mock.patch(_HEARTBEAT_LOOP, new=loop):
            result = await activity_fn(
                _task_context("_no-beat-app", "nobeat", heartbeating=False), _StallIn()
            )

        assert result.msg == "ok"
        assert loop_calls == []
