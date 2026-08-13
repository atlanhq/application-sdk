"""Unit tests for the run-length SLA observation (FND-294, ADR-0018).

The duration *alert* that replaces the duration *kill*: once ``start_to_close``
is a backstop, a run that wedges while dribbling progress re-arms the stall
watchdog on every mark and is bounded by nothing. These tests pin what the
observation reports, what it stays quiet about, and that it can never fail the
attempt it is only watching.

Clocks are injected rather than patched globally: an asyncio loop shares
``time.monotonic``, so patching it makes the loop itself misbehave.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.app import App
from application_sdk.app import base as base_module
from application_sdk.app.base import _create_task_activity_wrapper
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution import run_length as rl_mod
from application_sdk.execution._temporal import activities as activities_module
from application_sdk.execution._temporal.activities import (
    TaskContext,
    create_activity_from_task,
)
from application_sdk.execution._temporal.converter import create_data_converter
from application_sdk.execution.heartbeat import auto_heartbeat_loop
from application_sdk.execution.run_length import (
    _DEFAULT_SLA_SECONDS,
    _OBSERVE_INTERVAL_SECONDS,
    RunLengthWatch,
    _load_sla_seconds,
    build_run_length_watch,
)

_RUN_STARTED = 1_000_000.0
_SLA = 3600.0


@dataclass
class FakeClock:
    """A clock the test advances explicitly."""

    now: float

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Observed:
    """What a watch reported over its lifetime."""

    recorded: list[Any] = field(default_factory=list)
    warnings: list[Any] = field(default_factory=list)

    @property
    def ages(self) -> list[float]:
        return [call.args[0] for call in self.recorded]

    @property
    def attributes(self) -> dict[str, str]:
        assert len(self.recorded) == 1
        return self.recorded[0].args[1]


class _Harness:
    """One watch, its instrument and its logger, with time under test control."""

    def __init__(
        self,
        *,
        run_age: float,
        sla: float = _SLA,
        task_name: str = "extract",
        workflow_type: str = "MyConnectorWorkflow",
        run_started_at_epoch: float = _RUN_STARTED,
        record_raises: bool = False,
    ) -> None:
        self.monotonic = FakeClock(now=500.0)
        self.wall = FakeClock(now=run_started_at_epoch + run_age)
        self.histogram = MagicMock()
        if record_raises:
            self.histogram.record.side_effect = RuntimeError("metric backend down")
        self.watch = RunLengthWatch(
            run_started_at_epoch=run_started_at_epoch,
            sla_seconds=sla,
            task_name=task_name,
            workflow_type=workflow_type,
            clock=self.monotonic,
            wall_clock=self.wall,
        )

    def tick(self, *, after_seconds: float = 0.0) -> None:
        """Advance both clocks by the same amount, then observe once."""
        self.monotonic.advance(after_seconds)
        self.wall.advance(after_seconds)
        with (
            patch.object(rl_mod, "logger") as logger,
            patch.object(rl_mod, "_run_length_histogram", return_value=self.histogram),
        ):
            self.watch.observe()
        self._logger = logger

    def observed(self) -> Observed:
        return Observed(
            recorded=list(self.histogram.record.call_args_list),
            warnings=list(self._logger.warning.call_args_list),
        )


# ---------------------------------------------------------------------------
# What is, and is not, an observation
# ---------------------------------------------------------------------------


class TestObservation:
    def test_a_run_inside_its_sla_reports_nothing(self) -> None:
        harness = _Harness(run_age=_SLA - 1)
        harness.tick()

        observed = harness.observed()
        assert observed.recorded == []
        assert observed.warnings == []

    def test_a_run_at_exactly_its_sla_reports_nothing(self) -> None:
        """The load-bearing comparison is ``age <= sla_seconds``: a run at the
        exact boundary is still inside its SLA and must not alert."""
        harness = _Harness(run_age=_SLA)
        harness.tick()

        observed = harness.observed()
        assert observed.recorded == []
        assert observed.warnings == []

    def test_a_run_past_its_sla_records_its_age_and_warns(self) -> None:
        harness = _Harness(run_age=_SLA + 600)
        harness.tick()

        observed = harness.observed()
        assert observed.ages == [_SLA + 600]
        assert len(observed.warnings) == 1

    def test_the_metric_names_the_workflow_and_the_running_task(self) -> None:
        """An alert on the count has to say where the run was spending its time,
        not only that it was long."""
        harness = _Harness(run_age=_SLA + 1)
        harness.tick()

        assert harness.observed().attributes == {
            "task.name": "extract",
            "temporal.workflow.type": "MyConnectorWorkflow",
        }

    def test_the_first_tick_past_the_sla_reports_without_waiting_out_the_throttle(
        self,
    ) -> None:
        """A run inside its SLA is not an observation, so it must not consume the
        re-assert throttle on the way past the threshold."""
        harness = _Harness(run_age=_SLA - 10)
        harness.tick()
        assert harness.observed().recorded == []

        harness.tick(after_seconds=11)

        assert harness.observed().ages == [_SLA + 1]

    def test_an_unknown_run_start_reports_nothing(self) -> None:
        """A task invoked outside a workflow, or a run dispatched by a workflow
        that predates ``run_started_at_epoch`` and is in flight across the
        upgrade."""
        harness = _Harness(run_age=_SLA + 600, run_started_at_epoch=0.0)
        harness.tick()

        assert harness.observed().recorded == []

    def test_a_disabled_sla_reports_nothing(self) -> None:
        harness = _Harness(run_age=10 * _SLA, sla=0.0)
        harness.tick()

        assert harness.observed().recorded == []


# ---------------------------------------------------------------------------
# Re-assertion: the alert stays firing while the run does, and resolves after
# ---------------------------------------------------------------------------


class TestReAssertion:
    def test_the_metric_re_asserts_so_the_alert_keeps_firing(self) -> None:
        harness = _Harness(run_age=_SLA + 1)
        harness.tick()
        harness.tick(after_seconds=_OBSERVE_INTERVAL_SECONDS)
        harness.tick(after_seconds=_OBSERVE_INTERVAL_SECONDS)

        assert harness.observed().ages == [
            _SLA + 1,
            _SLA + 1 + _OBSERVE_INTERVAL_SECONDS,
            _SLA + 1 + 2 * _OBSERVE_INTERVAL_SECONDS,
        ]

    def test_ticks_inside_the_throttle_window_add_no_metric_points(self) -> None:
        """The heartbeat loop ticks every 10s by default; the alert needs minutes,
        not a point per beat."""
        harness = _Harness(run_age=_SLA + 1)
        harness.tick()
        for _ in range(5):
            harness.tick(after_seconds=_OBSERVE_INTERVAL_SECONDS / 6)

        assert len(harness.observed().recorded) == 1

    def test_the_warning_is_logged_once_per_attempt_not_once_per_point(self) -> None:
        """The metric is what an alert reads; a line per minute for three days
        would bury it."""
        harness = _Harness(run_age=_SLA + 1)
        harness.tick()
        first = harness.observed().warnings
        harness.tick(after_seconds=_OBSERVE_INTERVAL_SECONDS)
        second = harness.observed().warnings

        assert len(first) == 1
        assert second == []


# ---------------------------------------------------------------------------
# It can never fail the attempt it is watching
# ---------------------------------------------------------------------------


class TestBestEffort:
    def test_a_metric_backend_failure_is_swallowed_and_complained_about(self) -> None:
        harness = _Harness(run_age=_SLA + 1, record_raises=True)
        harness.tick()  # must not raise

        warnings = harness.observed().warnings
        assert any("Failed to observe the run length" in str(w) for w in warnings)


# ---------------------------------------------------------------------------
# Construction: SLA resolution and the cases with nothing to watch
# ---------------------------------------------------------------------------


class TestBuildRunLengthWatch:
    def test_builds_a_watch_for_a_known_run(self) -> None:
        watch = build_run_length_watch(_RUN_STARTED, task_name="extract")

        assert watch is not None
        assert watch.sla_seconds == rl_mod.RUN_LENGTH_SLA_SECONDS

    def test_no_watch_without_a_run_start(self) -> None:
        assert build_run_length_watch(None, task_name="extract") is None
        assert build_run_length_watch(0.0, task_name="extract") is None

    def test_no_watch_when_the_sla_is_disabled(self) -> None:
        assert build_run_length_watch(_RUN_STARTED, "extract", sla_seconds=0) is None

    def test_an_explicit_sla_overrides_the_process_wide_one(self) -> None:
        watch = build_run_length_watch(_RUN_STARTED, "extract", sla_seconds=60)

        assert watch is not None
        assert watch.sla_seconds == 60


class TestSlaFromEnv:
    def test_defaults_to_24h(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLAN_RUN_LENGTH_SLA_SECONDS", raising=False)

        assert _load_sla_seconds() == float(_DEFAULT_SLA_SECONDS) == 86_400.0

    def test_an_app_declares_its_own_run_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_RUN_LENGTH_SLA_SECONDS", "172800")

        assert _load_sla_seconds() == 172_800.0

    def test_zero_disables_the_alert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_RUN_LENGTH_SLA_SECONDS", "0")

        assert _load_sla_seconds() == 0.0

    def test_a_negative_value_disables_rather_than_alerting_on_every_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_RUN_LENGTH_SLA_SECONDS", "-1")

        assert _load_sla_seconds() == 0.0

    def test_a_malformed_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_RUN_LENGTH_SLA_SECONDS", "a day or so")

        assert _load_sla_seconds() == float(_DEFAULT_SLA_SECONDS)

    def test_the_import_time_constant_is_what_production_reads(self) -> None:
        """``build_run_length_watch`` defaults to ``RUN_LENGTH_SLA_SECONDS``,
        which is bound once at import — so the env var's effect on the constant
        itself is the surface worth pinning, not only the loader beneath it.

        Run in a subprocess: the constant binds at import, and re-importing the
        module in this process would mint a second ``RunLengthWatch`` class and
        break ``isinstance`` for every sibling test that imported it by name.
        """
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; "
                "os.environ['ATLAN_RUN_LENGTH_SLA_SECONDS'] = '7200'; "
                "from application_sdk.execution import run_length as m; "
                "assert m.RUN_LENGTH_SLA_SECONDS == 7200.0, "
                "f'constant bound {m.RUN_LENGTH_SLA_SECONDS}, not the env var'",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# The wiring: the watch rides the heartbeat tick
# ---------------------------------------------------------------------------


class TestHeartbeatLoopWiring:
    @pytest.mark.asyncio
    async def test_the_loop_observes_the_run_on_every_tick(self) -> None:
        stop = asyncio.Event()
        watch = MagicMock()
        beats: list[int] = []

        def beat() -> None:
            beats.append(1)
            if len(beats) >= 2:
                stop.set()

        await auto_heartbeat_loop(
            interval_seconds=0.001,
            heartbeat_fn=beat,
            stop_event=stop,
            task_name="extract",
            run_length=watch,
        )

        assert watch.observe.call_count >= 1

    @pytest.mark.asyncio
    async def test_no_watch_leaves_the_loop_as_it_was(self) -> None:
        """The default is byte-identical to before the observation existed."""
        stop = asyncio.Event()
        beats: list[int] = []

        def beat() -> None:
            beats.append(1)
            stop.set()

        await auto_heartbeat_loop(
            interval_seconds=0.001,
            heartbeat_fn=beat,
            stop_event=stop,
            task_name="extract",
        )

        assert beats == [1]


# ---------------------------------------------------------------------------
# The plumbing: the run start reaches the activity, and the watch reaches the tick
# ---------------------------------------------------------------------------


class _PlumbingIn(Input, allow_unbounded_fields=True):
    name: str = "x"


class _PlumbingOut(Output, allow_unbounded_fields=True):
    msg: str = ""


@pytest.fixture
def _clean_registries() -> Any:
    AppRegistry.reset()
    TaskRegistry.reset()
    yield
    AppRegistry.reset()
    TaskRegistry.reset()


@pytest.mark.usefixtures("_clean_registries")
class TestActivityPlumbing:
    """The activity is where the two halves meet: the run start arrives on the
    ``TaskContext``, and the watch has to reach the loop that ticks it."""

    async def _run_activity(self, *, run_started_at_epoch: float) -> Any:
        class _PlumbingApp(App):
            @task(timeout_seconds=600)
            async def extract(self, input: _PlumbingIn) -> _PlumbingOut:
                return _PlumbingOut(msg="done")

            async def run(self, input: _PlumbingIn) -> _PlumbingOut:
                return await self.extract(input)

        tasks = TaskRegistry.get_instance().get_tasks_for_app("_plumbing-app")
        activity_fn = create_activity_from_task(
            next(t for t in tasks if t.name == "extract")
        )
        context = TaskContext(
            app_name="_plumbing-app",
            task_name="extract",
            run_id="run-1",
            heartbeat_timeout_seconds=60,
            auto_heartbeat_seconds=10,
            run_started_at_epoch=run_started_at_epoch,
        )

        captured: dict[str, Any] = {}

        async def fake_loop(*, stop_event: asyncio.Event, **kwargs: Any) -> None:
            captured.update(kwargs)
            await stop_event.wait()

        with (
            patch(
                "application_sdk.execution.heartbeat.auto_heartbeat_loop",
                new=fake_loop,
            ),
            patch.object(
                activities_module.activity,
                "info",
                return_value=MagicMock(
                    workflow_id="wf-1", workflow_type="MyConnectorWorkflow"
                ),
            ),
            patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
        ):
            await activity_fn(context, _PlumbingIn())

        return captured.get("run_length")

    @pytest.mark.asyncio
    async def test_the_activity_hands_the_loop_a_watch_for_its_run(self) -> None:
        watch = await self._run_activity(run_started_at_epoch=_RUN_STARTED)

        assert isinstance(watch, RunLengthWatch)
        assert watch.run_started_at_epoch == _RUN_STARTED
        assert watch.task_name == "extract"
        assert watch.workflow_type == "MyConnectorWorkflow"

    @pytest.mark.asyncio
    async def test_no_watch_when_the_dispatching_workflow_sent_no_run_start(
        self,
    ) -> None:
        assert await self._run_activity(run_started_at_epoch=0.0) is None


@dataclass
class _OldTaskContext:
    """``TaskContext`` as a worker from before this field knows it."""

    app_name: str
    task_name: str
    run_id: str
    workflow_id: str = "local"
    heartbeat_timeout_seconds: int | None = 60
    auto_heartbeat_seconds: int | None = 20


class TestWireCompatibility:
    """A rolling deploy runs two SDK versions against one task queue for a few
    minutes, so a new workflow's dispatch can land on an old worker. Pinned
    against the real converter rather than assumed: if the premise ever changes,
    every activity dispatched during a deploy would fail on decode."""

    @pytest.mark.asyncio
    async def test_the_run_start_survives_the_wire(self) -> None:
        converter = create_data_converter()
        context = TaskContext(
            app_name="a", task_name="t", run_id="r", run_started_at_epoch=_RUN_STARTED
        )

        payloads = await converter.encode([context])
        decoded = await converter.decode(payloads, [TaskContext])

        assert decoded[0].run_started_at_epoch == _RUN_STARTED

    @pytest.mark.asyncio
    async def test_an_older_worker_ignores_the_field_rather_than_failing(self) -> None:
        converter = create_data_converter()
        context = TaskContext(
            app_name="a", task_name="t", run_id="r", run_started_at_epoch=_RUN_STARTED
        )

        payloads = await converter.encode([context])
        decoded = await converter.decode(payloads, [_OldTaskContext])

        assert decoded[0].task_name == "t"


class TestWorkflowStampsTheRunStart:
    async def _dispatched_context(self) -> Any:
        with patch(
            "application_sdk.execution._temporal.eviction_retry."
            "execute_activity_with_eviction_retry",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.return_value = MagicMock()
            wrapper = _create_task_activity_wrapper(
                app_name="qi-app",
                task_name="extract",
                timeout_seconds=600,
                retry_max_attempts=3,
                retry_max_interval_seconds=30,
                output_type=_PlumbingOut,
                context_data={"run_id": "r1", "correlation_id": "c1"},
            )
            await wrapper(MagicMock())

        return mock_exec.call_args.kwargs["args"][0]

    async def test_the_run_start_comes_from_history_not_a_clock(self) -> None:
        """``workflow.info().start_time`` is replayed from the run's own history,
        so a replay measures the run's real age instead of restarting the clock."""
        started = datetime(2026, 8, 13, 6, 0, tzinfo=UTC)

        with patch.object(
            base_module.workflow, "info", return_value=MagicMock(start_time=started)
        ):
            context = await self._dispatched_context()

        assert context.run_started_at_epoch == started.timestamp()

    async def test_unknown_outside_a_workflow_context(self) -> None:
        """A local run has no run to measure — and reading the start must never
        be the thing that fails a dispatch."""
        with patch.object(
            base_module.workflow,
            "info",
            side_effect=RuntimeError("not in workflow event loop"),
        ):
            context = await self._dispatched_context()

        assert context.run_started_at_epoch == 0.0
