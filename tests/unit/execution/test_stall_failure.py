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
3. **What the activity supplies to the watchdog.** Since FND-296 the mode and the
   budget are resolved in the activity and passed to the loop, and a task that
   declares nothing arrives in ``warn`` — the fleet default, with no opt-in.
   Resolution happens on the activity side so ``off`` in the worker's
   environment is a real kill-switch, and so a run dispatched before these
   fields existed lands on the default rather than on nothing. All of that is
   asserted rather than trusted.

The end-to-end test drives the *real* watchdog inside the *real* activity body,
with only the enforce config overridden — no patched clock (an asyncio loop
shares ``time.monotonic``), just a 10 ms tick against a 50 ms budget.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
from application_sdk.execution import progress as progress_module
from application_sdk.execution import progress_telemetry as telemetry_module
from application_sdk.execution import shutdown as shutdown_module
from application_sdk.execution._temporal import activities as activities_module
from application_sdk.execution._temporal.activities import (
    _WATCHDOG_ONLY_TICK_SECONDS,
    TaskContext,
    create_activity_from_task,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.execution.heartbeat import (
    NoopHeartbeatController,
    TemporalHeartbeatController,
)
from application_sdk.execution.heartbeat import (
    auto_heartbeat_loop as _real_auto_heartbeat_loop,
)
from application_sdk.execution.progress import (
    ClosedHold,
    ProgressTracker,
    ProgressWatchdogMode,
    current_progress_tracker,
    holding_progress,
    resolve_watchdog_mode,
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


_HEARTBEAT_DEFAULT = object()


def _task_context(
    app_name: str,
    task_name: str,
    *,
    heartbeating: bool = True,
    heartbeat_timeout_seconds: int | None | object = _HEARTBEAT_DEFAULT,
    auto_heartbeat_seconds: int | None | object = _HEARTBEAT_DEFAULT,
    progress_watchdog: ProgressWatchdogMode | None = None,
    max_no_progress_seconds: float | None = None,
) -> TaskContext:
    return TaskContext(
        app_name=app_name,
        task_name=task_name,
        run_id="run-1",
        heartbeat_timeout_seconds=(
            (60 if heartbeating else None)
            if heartbeat_timeout_seconds is _HEARTBEAT_DEFAULT
            else cast("int | None", heartbeat_timeout_seconds)
        ),
        auto_heartbeat_seconds=(
            (10 if heartbeating else None)
            if auto_heartbeat_seconds is _HEARTBEAT_DEFAULT
            else cast("int | None", auto_heartbeat_seconds)
        ),
        progress_watchdog=progress_watchdog,
        max_no_progress_seconds=max_no_progress_seconds,
    )


def _enforcing_watchdog(
    *, budget_seconds: float = 0.05, tick_seconds: float = 0.01
) -> Callable[..., Awaitable[None]]:
    """The real auto-heartbeat loop, sped up and pinned to ``enforce``.

    Everything under test stays real — the tick, the gap arithmetic, the
    ``on_stall`` call, the loop returning immediately after enforcing. The three
    values ``activities.py`` supplies are *overridden* rather than added: since
    FND-296 it does pass a mode and a budget, and a test that waited for the
    fleet default (``warn``, 900s) would neither enforce nor finish this decade.
    That the real wiring passes the resolved pair at all is asserted separately,
    in :meth:`TestWatchdogWiring.test_the_activity_supplies_the_resolved_config`.
    """

    async def loop(**kwargs: Any) -> None:
        kwargs["interval_seconds"] = tick_seconds
        kwargs["max_no_progress_seconds"] = budget_seconds
        kwargs["watchdog_mode"] = ProgressWatchdogMode.ENFORCE
        await _real_auto_heartbeat_loop(**kwargs)

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

    async def test_the_activity_supplies_the_resolved_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A task that declares nothing still arrives at the loop in warn mode.

        The whole of FND-296 in one assertion: no opt-in, no per-app step, and
        the mode reaching the watchdog is the fleet default rather than ``OFF``.
        The budget comes with it, since a mode with no allowance is inert.

        Both constants are pinned rather than read from the environment: a worker
        started with ``ATLAN_PROGRESS_WATCHDOG=enforce`` (or a custom allowance)
        would otherwise make this assert a non-default.
        """
        monkeypatch.setattr(
            progress_module, "PROGRESS_WATCHDOG_MODE", ProgressWatchdogMode.WARN
        )
        monkeypatch.setattr(progress_module, "MAX_NO_PROGRESS_SECONDS", 900.0)
        seen: dict[str, Any] = {}

        class _InheritingApp(App):
            @task
            async def inherits(self, input: _StallIn) -> _StallOut:
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.inherits(input)

        async def loop(*, stop_event: asyncio.Event, **kwargs: Any) -> None:
            seen.update(kwargs)
            await stop_event.wait()

        _InheritingApp()
        activity_fn = _activity_for("_inheriting-app", "inherits")

        with mock.patch(_HEARTBEAT_LOOP, new=loop):
            await activity_fn(_task_context("_inheriting-app", "inherits"), _StallIn())

        assert seen["watchdog_mode"] is ProgressWatchdogMode.WARN
        assert seen["max_no_progress_seconds"] == 900.0

    async def test_the_hold_observer_gets_the_resolved_allowance(self) -> None:
        """A declared ``max_no_progress_seconds`` reaches the hold observer too.

        The observer's ``budget_seconds`` is the floor above which an unbounded
        hold is worth naming; left at the default it would ignore the task's own
        allowance, under-reporting holds past a smaller budget and over-reporting
        ones under a larger one.
        """
        built: dict[str, Any] = {}

        def observer(task_name: str, *, budget_seconds: float) -> None:
            built["task_name"] = task_name
            built["budget_seconds"] = budget_seconds
            return None

        class _DeclaringApp(App):
            @task(max_no_progress_seconds=42)
            async def declares(self, input: _StallIn) -> _StallOut:
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.declares(input)

        async def loop(*, stop_event: asyncio.Event, **_kwargs: Any) -> None:
            await stop_event.wait()

        _DeclaringApp()
        activity_fn = _activity_for("_declaring-app", "declares")

        # ``closed_hold_observer`` is imported function-locally from
        # ``progress_telemetry`` (a circular-import workaround), so the patch has
        # to land on the source module, not on ``activities``.
        with (
            mock.patch(_HEARTBEAT_LOOP, new=loop),
            mock.patch.object(
                telemetry_module, "closed_hold_observer", side_effect=observer
            ),
        ):
            await activity_fn(
                _task_context(
                    "_declaring-app", "declares", max_no_progress_seconds=42.0
                ),
                _StallIn(),
            )

        assert built["budget_seconds"] == 42.0

    async def test_a_declaration_on_the_wire_reaches_the_loop(self) -> None:
        """What an app flips in rollout step 6, arriving from the dispatch side."""
        seen: dict[str, Any] = {}

        class _EnforcingApp(App):
            @task(progress_watchdog="enforce", max_no_progress_seconds=42)
            async def strict(self, input: _StallIn) -> _StallOut:
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.strict(input)

        async def loop(*, stop_event: asyncio.Event, **kwargs: Any) -> None:
            seen.update(kwargs)
            await stop_event.wait()

        _EnforcingApp()
        activity_fn = _activity_for("_enforcing-app", "strict")

        with mock.patch(_HEARTBEAT_LOOP, new=loop):
            await activity_fn(
                _task_context(
                    "_enforcing-app",
                    "strict",
                    progress_watchdog=ProgressWatchdogMode.ENFORCE,
                    max_no_progress_seconds=42.0,
                ),
                _StallIn(),
            )

        assert seen["watchdog_mode"] is ProgressWatchdogMode.ENFORCE
        assert seen["max_no_progress_seconds"] == 42.0

    async def test_the_kill_switch_is_read_on_the_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``off`` in the activity worker's environment beats a per-task ``enforce``.

        Resolution happens here rather than on the workflow side precisely so an
        operator can throw the switch without every in-flight run needing to be
        re-dispatched to notice. A resolved ``off`` starts no loop at all — the
        one mode that is genuinely inert for no-progress *detection*.
        """
        loop_calls: list[dict[str, Any]] = []

        class _PinnedApp(App):
            @task(progress_watchdog="enforce")
            async def pinned(self, input: _StallIn) -> _StallOut:
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.pinned(input)

        async def loop(*, stop_event: asyncio.Event, **kwargs: Any) -> None:
            loop_calls.append(kwargs)
            await stop_event.wait()

        _PinnedApp()
        activity_fn = _activity_for("_pinned-app", "pinned")
        monkeypatch.setattr(
            progress_module, "PROGRESS_WATCHDOG_MODE", ProgressWatchdogMode.OFF
        )

        with mock.patch(_HEARTBEAT_LOOP, new=loop):
            await activity_fn(
                _task_context(
                    "_pinned-app",
                    "pinned",
                    progress_watchdog=ProgressWatchdogMode.ENFORCE,
                ),
                _StallIn(),
            )

        assert loop_calls == []

    async def test_no_heartbeat_still_runs_the_watchdog(self) -> None:
        """A task with heartbeating disabled is exactly the one the watchdog protects.

        The seam gates on the resolved mode, not the heartbeat config: with the
        fleet default (``warn``) and no heartbeat, the loop still starts — riding
        a NoopHeartbeatController beat that emits no Temporal heartbeat — so a
        wedge is observed rather than left to the duration backstop alone.
        """
        loop_calls: list[dict[str, Any]] = []

        class _NoBeatApp(App):
            @task(timeout_seconds=600)
            async def nobeat(self, input: _StallIn) -> _StallOut:
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.nobeat(input)

        async def loop(*, stop_event: asyncio.Event, **kwargs: Any) -> None:
            loop_calls.append(kwargs)
            await stop_event.wait()

        activity_fn = _activity_for("_no-beat-app", "nobeat")

        with mock.patch(_HEARTBEAT_LOOP, new=loop):
            result = await activity_fn(
                _task_context("_no-beat-app", "nobeat", heartbeating=False), _StallIn()
            )

        assert result.msg == "ok"
        # One loop, in the inherited warn mode, ticking on the watchdog-only beat
        # (no heartbeat interval to reuse) and heartbeating through a Noop beat —
        # so nothing reaches Temporal, but a gap is still observed.
        assert len(loop_calls) == 1
        assert loop_calls[0]["watchdog_mode"] is ProgressWatchdogMode.WARN
        assert loop_calls[0]["interval_seconds"] == _WATCHDOG_ONLY_TICK_SECONDS
        assert isinstance(
            loop_calls[0]["heartbeat_fn"].__self__, NoopHeartbeatController
        )

    async def test_manual_heartbeats_get_no_automatic_keepalive(self) -> None:
        """Manual-heartbeat tasks opted out of auto-heartbeats, watchdog or not.

        The seam must not widen: ``heartbeat_timeout_seconds`` set with
        ``auto_heartbeat_seconds=None`` means *manual* heartbeating — the task's
        own ``heartbeat()`` calls, on the real Temporal controller — and the
        loop the watchdog rides must not add automatic keepalives the author
        disabled. So the loop ticks through a Noop beat while the controller
        handed to the task stays Temporal.
        """
        loop_calls: list[dict[str, Any]] = []

        class _ManualApp(App):
            @task(timeout_seconds=600)
            async def manual(self, input: _StallIn) -> _StallOut:
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.manual(input)

        async def loop(*, stop_event: asyncio.Event, **kwargs: Any) -> None:
            loop_calls.append(kwargs)
            await stop_event.wait()

        _ManualApp()
        activity_fn = _activity_for("_manual-app", "manual")

        with mock.patch(_HEARTBEAT_LOOP, new=loop):
            result = await activity_fn(
                _task_context(
                    "_manual-app",
                    "manual",
                    heartbeat_timeout_seconds=60,
                    auto_heartbeat_seconds=None,
                ),
                _StallIn(),
            )

        assert result.msg == "ok"
        # One loop — the watchdog still runs in the inherited warn mode — but
        # ticking on the watchdog-only beat through a Noop heartbeat, so no
        # automatic Temporal keepalive is emitted for a task that asked for
        # none.
        assert len(loop_calls) == 1
        assert loop_calls[0]["watchdog_mode"] is ProgressWatchdogMode.WARN
        assert loop_calls[0]["interval_seconds"] == _WATCHDOG_ONLY_TICK_SECONDS
        assert isinstance(
            loop_calls[0]["heartbeat_fn"].__self__, NoopHeartbeatController
        )

    async def test_auto_heartbeat_keeps_the_temporal_beat(self) -> None:
        """The ordinary path is untouched: auto-heartbeating rides the real beat.

        With an auto-heartbeat interval configured the loop must keep emitting
        keepalives through the task's own Temporal controller — the beat is the
        crash detector, and selecting the Noop here would silently blind it.
        """
        loop_calls: list[dict[str, Any]] = []

        class _BeatApp(App):
            @task(timeout_seconds=600)
            async def beat(self, input: _StallIn) -> _StallOut:
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.beat(input)

        async def loop(*, stop_event: asyncio.Event, **kwargs: Any) -> None:
            loop_calls.append(kwargs)
            await stop_event.wait()

        _BeatApp()
        activity_fn = _activity_for("_beat-app", "beat")

        with mock.patch(_HEARTBEAT_LOOP, new=loop):
            result = await activity_fn(
                _task_context(
                    "_beat-app",
                    "beat",
                    heartbeat_timeout_seconds=60,
                    auto_heartbeat_seconds=10,
                ),
                _StallIn(),
            )

        assert result.msg == "ok"
        assert len(loop_calls) == 1
        assert loop_calls[0]["interval_seconds"] == 10
        assert isinstance(
            loop_calls[0]["heartbeat_fn"].__self__, TemporalHeartbeatController
        )


class TestDeclarationReachesTheWire:
    """The dispatch half: a ``@task`` declaration has to survive the trip.

    ``TaskMetadata`` → ``_create_task_activity_wrapper`` → ``TaskContext`` →
    (Temporal) → the activity. The activity end is covered above; this is
    everything before it.
    """

    async def _dispatched_context(
        self,
        *,
        progress_watchdog: ProgressWatchdogMode | None = None,
        max_no_progress_seconds: float | None = None,
    ) -> TaskContext:
        from application_sdk.app.base import (  # noqa: PLC0415 — private dispatch seam, imported where it is used
            _create_task_activity_wrapper,
        )

        with mock.patch(
            "application_sdk.execution._temporal.eviction_retry."
            "execute_activity_with_eviction_retry",
            new_callable=mock.AsyncMock,
        ) as dispatched:
            dispatched.return_value = mock.MagicMock()
            wrapper = _create_task_activity_wrapper(
                app_name="_dispatch-app",
                task_name="extract",
                timeout_seconds=600,
                retry_max_attempts=3,
                retry_max_interval_seconds=30,
                output_type=_StallOut,
                context_data={"run_id": "r1"},
                progress_watchdog=progress_watchdog,
                max_no_progress_seconds=max_no_progress_seconds,
            )
            await wrapper(mock.MagicMock())

        return cast("TaskContext", dispatched.call_args.kwargs["args"][0])

    async def test_a_task_that_declares_nothing_sends_nothing(self) -> None:
        """``None`` on the wire, not a resolved ``warn``.

        The distinction is the kill-switch: a resolved mode baked in at dispatch
        would make an operator's ``off`` on the worker a no-op for every run
        already in flight.
        """
        context = await self._dispatched_context()

        assert context.progress_watchdog is None
        assert context.max_no_progress_seconds is None

    async def test_a_declaration_is_carried_verbatim(self) -> None:
        context = await self._dispatched_context(
            progress_watchdog=ProgressWatchdogMode.ENFORCE,
            max_no_progress_seconds=42.0,
        )

        assert context.progress_watchdog is ProgressWatchdogMode.ENFORCE
        assert context.max_no_progress_seconds == 42.0

    async def test_the_declaration_survives_the_converter(self) -> None:
        """Pinned against the real converter, not assumed.

        A ``StrEnum`` on an activity argument is a new shape for ``TaskContext``,
        and every dispatch would fail on decode if it did not round-trip.
        """
        from application_sdk.execution._temporal.converter import (  # noqa: PLC0415 — cold path, imported where it is used
            create_data_converter,
        )

        converter = create_data_converter()
        context = TaskContext(
            app_name="a",
            task_name="t",
            run_id="r",
            progress_watchdog=ProgressWatchdogMode.ENFORCE,
            max_no_progress_seconds=42.0,
        )

        payloads = await converter.encode([context])
        decoded = await converter.decode(payloads, [TaskContext])

        assert decoded[0].progress_watchdog is ProgressWatchdogMode.ENFORCE
        assert decoded[0].max_no_progress_seconds == 42.0

    async def test_a_dispatch_from_before_these_fields_lands_on_the_default(
        self,
    ) -> None:
        """A run in flight across the upgrade decodes to "declares nothing".

        Which is the whole reason the wire carries the declaration rather than
        the resolved mode: that run starts producing warn-mode telemetry as soon
        as the *worker* is upgraded, with no re-dispatch.
        """
        from application_sdk.execution._temporal.converter import (  # noqa: PLC0415 — cold path, imported where it is used
            create_data_converter,
        )

        @dataclasses.dataclass
        class _OldTaskContext:
            """``TaskContext`` as a worker from before FND-296 built it."""

            app_name: str
            task_name: str
            run_id: str

        converter = create_data_converter()
        payloads = await converter.encode(
            [_OldTaskContext(app_name="a", task_name="t", run_id="r")]
        )
        decoded = await converter.decode(payloads, [TaskContext])

        assert decoded[0].progress_watchdog is None
        assert resolve_watchdog_mode(decoded[0].progress_watchdog) is (
            ProgressWatchdogMode.WARN
        )


class TestOffIsNotFullyInert:
    """``off`` stops the watchdog, not every observation — and not the exposure.

    Documented in three places (the enum, Progress & Stalls, the stalled-task
    runbook) because it is the kind of claim an operator acts on mid-incident.
    Pinned here so the docs and the code cannot drift apart: someone "fixing"
    ``off`` to be genuinely inert would silently drop the hold work-list, and
    someone reading ``off`` as a way to shorten a wedge would be reaching for a
    lever that does not move.
    """

    async def _run_in_off_mode(self) -> tuple[list[dict[str, Any]], list[ClosedHold]]:
        """Run one attempt in ``off`` mode; the task body takes and releases a hold.

        Returns every call made to the heartbeat loop, and every hold observation
        that actually reached the telemetry layer.
        """
        loop_calls: list[dict[str, Any]] = []
        observed: list[ClosedHold] = []

        class _OffApp(App):
            @task
            async def quiet_step(self, input: _StallIn) -> _StallOut:
                # The shape every `run_in_thread` offload produces: one hold,
                # entered and released inside the attempt.
                async with holding_progress("vendor.export", timeout=None):
                    pass
                return _StallOut(msg="ok")

            async def run(self, input: _StallIn) -> _StallOut:
                return await self.quiet_step(input)

        async def loop(*, stop_event: asyncio.Event, **kwargs: Any) -> None:
            loop_calls.append(kwargs)
            await stop_event.wait()

        _OffApp()
        activity_fn = _activity_for("_off-app", "quiet_step")

        with (
            mock.patch(_HEARTBEAT_LOOP, new=loop),
            mock.patch.object(
                telemetry_module,
                "record_closed_hold",
                side_effect=lambda closed, **_kw: observed.append(closed),
            ),
        ):
            await activity_fn(
                _task_context(
                    "_off-app", "quiet_step", progress_watchdog=ProgressWatchdogMode.OFF
                ),
                _StallIn(),
            )

        return loop_calls, observed

    async def test_the_watchdog_never_acts(self) -> None:
        """``off`` starts no watchdog loop at all, so no gap is ever measured and
        ``on_stall`` can never be called.

        The seam gates on the resolved mode now, not the heartbeat config: a
        resolved ``off`` is the one mode that starts nothing, which is what makes
        it a real kill-switch for no-progress *detection*."""
        loop_calls, _ = await self._run_in_off_mode()

        assert loop_calls == []

    async def test_hold_telemetry_still_records(self) -> None:
        """The observer is attached at ``bind_progress_tracker``, not gated on mode.

        So ``task_hold_duration_seconds`` keeps recording once per released hold
        — once per ``run_in_thread`` offload — which is why ``off`` is not
        byte-identical to pre-ADR-0018 behaviour, and why the docs no longer say
        it is.
        """
        _, observed = await self._run_in_off_mode()

        assert [h.label for h in observed] == ["vendor.export"]
