"""Unit tests for SizingTelemetryInterceptor and the observation record."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.execution._temporal.interceptors.sizing import (
    WILDCARD,
    SizingTelemetryInterceptor,
    _SizingActivityInboundInterceptor,
)
from application_sdk.execution.settings import load_interceptor_settings
from application_sdk.observability import sizing as sizing_module
from application_sdk.observability.cgroup import ContainerTrace
from application_sdk.observability.sizing import SizingObservation

_ACTIVITY_TARGET = "application_sdk.execution._temporal.interceptors.sizing.activity"
_TRACKER_TARGET = (
    "application_sdk.execution._temporal.interceptors.sizing.track_container_usage"
)
_RECORD_TARGET = (
    "application_sdk.execution._temporal.interceptors.sizing.record_observation"
)
_METER_TARGET = "application_sdk.observability.sizing._otel_metrics.get_meter"


@dataclass
class MockActivityInfo:
    activity_type: str = "merge"
    activity_id: str = "act-id"
    task_queue: str = "app-worker"
    workflow_id: str = "wf-id"
    workflow_run_id: str = "run-id"
    workflow_type: str = "MergeWorkflow"
    attempt: int = 1


@dataclass
class MockExecuteActivityInput:
    headers: dict = field(default_factory=dict)
    args: list = field(default_factory=list)


def _tracker(trace: ContainerTrace, *, fill_on_exit: ContainerTrace | None = None):
    """A stand-in for ``track_container_usage``.

    ``fill_on_exit`` reproduces the real contract — the tracker fills the trace in
    *its* ``finally`` — so a double that pre-filled it would hide the ordering bug.
    """
    import contextlib

    @contextlib.asynccontextmanager
    async def _cm(poll_interval_seconds: float = 1.0):
        try:
            yield trace
        finally:
            if fill_on_exit is not None:
                trace.peak_memory_bytes = fill_on_exit.peak_memory_bytes
                trace.peak_memory_fraction = fill_on_exit.peak_memory_fraction
                trace.peak_source = fill_on_exit.peak_source
                trace.cpu_seconds = fill_on_exit.cpu_seconds

    return _cm


class TestSizingObservation:
    def test_mean_cpu_cores(self):
        obs = SizingObservation(
            activity_type="merge",
            task_queue="q",
            workflow_type="W",
            attempt=1,
            outcome="OK",
            duration_seconds=20.0,
            cpu_seconds=30.0,
        )
        assert obs.mean_cpu_cores == pytest.approx(1.5)

    def test_mean_cpu_cores_none_without_cpu(self):
        obs = SizingObservation(
            activity_type="merge",
            task_queue="q",
            workflow_type="W",
            attempt=1,
            outcome="OK",
            duration_seconds=20.0,
        )
        assert obs.mean_cpu_cores is None

    def test_mean_cpu_cores_none_for_zero_duration(self):
        """No divide-by-zero on a sub-tick activity."""
        obs = SizingObservation(
            activity_type="merge",
            task_queue="q",
            workflow_type="W",
            attempt=1,
            outcome="OK",
            duration_seconds=0.0,
            cpu_seconds=1.0,
        )
        assert obs.mean_cpu_cores is None

    def test_has_data_is_false_when_nothing_was_measured(self):
        """A non-cgroup host produces no row — a null read as zero fits the
        smallest tier to an activity nobody measured."""
        obs = SizingObservation(
            activity_type="merge",
            task_queue="q",
            workflow_type="W",
            attempt=1,
            outcome="OK",
            duration_seconds=1.0,
        )
        assert obs.has_data() is False

    def test_has_data_is_true_with_cpu_only(self):
        obs = SizingObservation(
            activity_type="merge",
            task_queue="q",
            workflow_type="W",
            attempt=1,
            outcome="OK",
            duration_seconds=1.0,
            cpu_seconds=0.5,
        )
        assert obs.has_data() is True

    def test_from_trace_carries_peak_source_and_throttling(self):
        trace = ContainerTrace(
            peak_memory_bytes=6 * 1024**3,
            peak_memory_fraction=0.75,
            peak_source="poll",
            memory_limit_bytes=8 * 1024**3,
            cpu_seconds=12.0,
            cpu_throttled_seconds=3.0,
            cpu_periods=100,
            cpu_throttled_periods=25,
            cpu_quota_cores=2.0,
        )
        obs = SizingObservation.from_trace(
            trace,
            activity_type="merge",
            task_queue="q",
            workflow_type="W",
            attempt=2,
            outcome="OK",
            duration_seconds=10.0,
        )
        assert obs.peak_source == "poll"
        assert obs.cpu_throttled_fraction == pytest.approx(0.25)
        assert obs.cpu_quota_cores == 2.0
        assert obs.attempt == 2


class TestRecordObservation:
    @pytest.fixture
    def mock_meter(self):
        sizing_module._INSTRUMENTS.clear()
        m = MagicMock()
        with patch(_METER_TARGET, return_value=m):
            yield m
        sizing_module._INSTRUMENTS.clear()

    def _obs(self, **overrides):
        base = {
            "activity_type": "merge",
            "task_queue": "app-worker",
            "workflow_type": "MergeWorkflow",
            "attempt": 1,
            "outcome": "OK",
            "duration_seconds": 10.0,
            "peak_memory_bytes": 2 * 1024**3,
            "peak_memory_fraction": 0.5,
            "peak_source": "watermark",
            "memory_limit_bytes": 4 * 1024**3,
            "cpu_seconds": 5.0,
            "cpu_throttled_fraction": 0.1,
            "cpu_quota_cores": 1.0,
        }
        base.update(overrides)
        return SizingObservation(**base)

    def test_records_peak_in_mib(self, mock_meter):
        sizing_module.record_observation(self._obs())
        hist = mock_meter.create_histogram.return_value
        recorded = [c[0][0] for c in hist.record.call_args_list]
        assert 2048.0 in recorded

    def test_labels_are_bounded(self, mock_meter):
        """No workflow_id — a UUID would blow up every histogram."""
        sizing_module.record_observation(self._obs())
        hist = mock_meter.create_histogram.return_value
        attrs = hist.record.call_args_list[0][0][1]
        assert set(attrs) == {
            "activity.type",
            "temporal.task_queue",
            "outcome",
            "peak.source",
        }

    def test_emits_nothing_when_there_is_no_data(self, mock_meter):
        sizing_module.record_observation(
            self._obs(
                peak_memory_bytes=None,
                peak_memory_fraction=None,
                cpu_seconds=None,
                cpu_throttled_fraction=None,
            )
        )
        mock_meter.create_histogram.return_value.record.assert_not_called()

    def test_never_raises_when_the_meter_is_broken(self, mock_meter):
        """Called from an activity's finally; raising would replace its outcome."""
        mock_meter.create_histogram.side_effect = RuntimeError("otel down")
        sizing_module.record_observation(self._obs())  # must not raise

    def test_logs_one_json_line_with_the_marker(self, mock_meter):
        with patch.object(sizing_module, "_logger") as mock_log:
            sizing_module.record_observation(self._obs())
        msg, payload = mock_log.info.call_args[0]
        assert "activity_sizing_observation" in msg
        import orjson

        parsed = orjson.loads(payload)
        assert parsed["activity_type"] == "merge"
        assert parsed["peak_source"] == "watermark"
        assert parsed["mean_cpu_cores"] == pytest.approx(0.5)


class TestSizingInterceptor:
    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_activity = AsyncMock(return_value="ok")
        return n

    async def test_returns_the_activity_result(self, mock_next):
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({"merge"})
        )
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET, _tracker(ContainerTrace())),
            patch(_RECORD_TARGET),
        ):
            mock_act.info.return_value = MockActivityInfo()
            assert (
                await interceptor.execute_activity(MockExecuteActivityInput()) == "ok"
            )

    async def test_reads_the_trace_after_the_tracker_exits(self, mock_next):
        """Recording inside the ``async with`` would capture an empty trace and
        null every collected peak."""
        filled = ContainerTrace(
            peak_memory_bytes=9 * 1024**3,
            peak_memory_fraction=0.9,
            peak_source="watermark",
            cpu_seconds=4.0,
        )
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(
                _TRACKER_TARGET,
                _tracker(ContainerTrace(), fill_on_exit=filled),
            ),
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo()
            await interceptor_execute(mock_next)

        obs = mock_record.call_args[0][0]
        assert obs.peak_memory_bytes == 9 * 1024**3
        assert obs.peak_source == "watermark"

    async def test_tags_a_failure_as_error_and_still_records(self, mock_next):
        """A failed activity is the most sizing-relevant sample there is."""
        mock_next.execute_activity = AsyncMock(side_effect=ValueError("boom"))
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({"merge"})
        )
        trace = ContainerTrace(peak_memory_bytes=1024, peak_source="poll")
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET, _tracker(trace)),
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo()
            with pytest.raises(ValueError, match="boom"):
                await interceptor.execute_activity(MockExecuteActivityInput())

        obs = mock_record.call_args[0][0]
        assert obs.outcome == "ERROR"
        assert obs.peak_memory_bytes == 1024

    async def test_carries_the_activity_identity(self, mock_next):
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({"merge"})
        )
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET, _tracker(ContainerTrace(peak_memory_bytes=1))),
            patch(_RECORD_TARGET) as mock_record,
        ):
            interceptor._activities = frozenset({"fetch_entities"})
            mock_act.info.return_value = MockActivityInfo(
                activity_type="fetch_entities", task_queue="heavy", attempt=3
            )
            await interceptor.execute_activity(MockExecuteActivityInput())

        obs = mock_record.call_args[0][0]
        assert obs.activity_type == "fetch_entities"
        assert obs.task_queue == "heavy"
        assert obs.attempt == 3
        assert obs.duration_seconds >= 0

    async def test_poll_interval_is_passed_through(self, mock_next):
        captured = {}

        import contextlib

        @contextlib.asynccontextmanager
        async def _cm(poll_interval_seconds: float = 1.0):
            captured["interval"] = poll_interval_seconds
            yield ContainerTrace()

        interceptor = SizingTelemetryInterceptor(
            poll_interval_seconds=0.25, activities=frozenset({"merge"})
        ).intercept_activity(mock_next)
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET, _cm),
            patch(_RECORD_TARGET),
        ):
            mock_act.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(MockExecuteActivityInput())

        assert captured["interval"] == 0.25


class TestAgainstTheRealTracker:
    """The doubles above pin the contract; this pins the wiring.

    Everything in ``TestSizingInterceptor`` patches the tracker, so all of it would
    pass if tracker and interceptor disagreed on when the trace is complete.
    """

    async def test_a_peak_reaches_the_observation(self, tmp_path, monkeypatch):
        from application_sdk.observability import cgroup

        peak = tmp_path / "memory.peak"
        peak.write_text("5000")
        current = tmp_path / "memory.current"
        current.write_text("1000")
        limit = tmp_path / "memory.max"
        limit.write_text("20000")
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", (str(peak),))
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", (str(current),))
        monkeypatch.setattr(cgroup, "_MEMORY_LIMIT_PATHS", (str(limit),))

        async def _work(_input):
            peak.write_text("15000")  # the kernel recording a spike mid-activity
            return "ok"

        nxt = AsyncMock()
        nxt.execute_activity = _work
        interceptor = _SizingActivityInboundInterceptor(nxt, 0, frozenset({"merge"}))

        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo()
            assert (
                await interceptor.execute_activity(MockExecuteActivityInput()) == "ok"
            )

        obs = mock_record.call_args[0][0]
        assert obs.peak_memory_bytes == 15000
        assert obs.peak_memory_fraction == pytest.approx(0.75)
        assert obs.peak_source == "watermark"
        assert obs.has_data() is True

    async def test_no_cgroup_produces_no_row(self, tmp_path, monkeypatch):
        """Nothing measured must mean nothing recorded."""
        from application_sdk.observability import cgroup

        missing = (str(tmp_path / "nope"),)
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", missing)
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", missing)
        monkeypatch.setattr(cgroup, "_MEMORY_LIMIT_PATHS", missing)
        monkeypatch.setattr(cgroup, "_CPU_STAT_V2", str(tmp_path / "nope"))
        monkeypatch.setattr(cgroup, "_CPU_STAT_V1", str(tmp_path / "nope"))
        monkeypatch.delenv("K8S_POD_MEMORY_LIMIT", raising=False)

        nxt = AsyncMock()
        nxt.execute_activity = AsyncMock(return_value="ok")
        interceptor = _SizingActivityInboundInterceptor(nxt, 1.0, frozenset({"merge"}))

        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch.object(sizing_module, "_logger") as mock_log,
            patch(_METER_TARGET) as mock_meter,
        ):
            mock_act.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(MockExecuteActivityInput())

        mock_log.info.assert_not_called()
        mock_meter.assert_not_called()


class TestInputSizeReachesTheRecord:
    """The driver variable has to arrive on the same row as the peak."""

    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_activity = AsyncMock(return_value="ok")
        return n

    def _input_with(self, tmp_path, size: int):
        from pydantic import BaseModel

        from application_sdk.contracts.types import FileReference

        f = tmp_path / "in.parquet"
        f.write_bytes(b"x" * size)

        class _In(BaseModel):
            ref: FileReference

        # args[0] is TaskContext, args[1] is the Input — matching the v3
        # activity signature the interceptor reads.
        return MockExecuteActivityInput(
            args=[object(), _In(ref=FileReference(local_path=str(f)))]
        )

    async def _run(self, mock_next, activity_input):
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({"merge"})
        )
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET, _tracker(ContainerTrace(peak_memory_bytes=3000))),
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo(activity_type="merge")
            await interceptor.execute_activity(activity_input)
        return mock_record.call_args[0][0]

    async def test_file_reference_size_is_recorded(self, mock_next, tmp_path):
        obs = await self._run(mock_next, self._input_with(tmp_path, 1000))
        assert obs.input_bytes == 1000
        assert obs.input_basis == "file_reference"
        assert obs.peak_per_input_byte == pytest.approx(3.0)

    async def test_no_input_args_is_not_an_error(self, mock_next):
        """Activities with no Input still produce a peak row, just no driver."""
        obs = await self._run(mock_next, MockExecuteActivityInput())
        assert obs.input_bytes is None
        assert obs.peak_memory_bytes == 3000

    async def test_unsizeable_input_still_records_the_peak(self, mock_next):
        """An input the sizer cannot read must not cost the peak measurement."""
        obs = await self._run(
            mock_next, MockExecuteActivityInput(args=[object(), "not a model"])
        )
        assert obs.input_bytes is None
        assert obs.peak_memory_bytes == 3000


async def interceptor_execute(mock_next):
    interceptor = _SizingActivityInboundInterceptor(
        mock_next, 1.0, frozenset({"merge"})
    )
    return await interceptor.execute_activity(MockExecuteActivityInput())


class TestActivityAllowList:
    """Only the activities a dev names get measured."""

    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_activity = AsyncMock(return_value="ok")
        return n

    async def test_unselected_activity_is_never_measured(self, mock_next):
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({"merge"})
        )
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET) as mock_tracker,
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo(activity_type="write_state")
            assert (
                await interceptor.execute_activity(MockExecuteActivityInput()) == "ok"
            )

        # Asserting on the tracker, not the record: filtering later would also
        # record nothing while still paying setup on every activity.
        mock_tracker.assert_not_called()
        mock_record.assert_not_called()

    async def test_selected_activity_is_measured(self, mock_next):
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({"merge", "fetch_entities"})
        )
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET, _tracker(ContainerTrace(peak_memory_bytes=1))),
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo(activity_type="merge")
            await interceptor.execute_activity(MockExecuteActivityInput())
        mock_record.assert_called_once()

    async def test_empty_allow_list_measures_nothing(self, mock_next):
        """Fails closed: attached but empty must be inert."""
        interceptor = _SizingActivityInboundInterceptor(mock_next, 1.0, frozenset())
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET) as mock_tracker,
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo(activity_type="merge")
            await interceptor.execute_activity(MockExecuteActivityInput())
        mock_tracker.assert_not_called()
        mock_record.assert_not_called()

    async def test_wildcard_measures_everything(self, mock_next):
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({WILDCARD})
        )
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET, _tracker(ContainerTrace(peak_memory_bytes=1))),
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo(activity_type="anything")
            await interceptor.execute_activity(MockExecuteActivityInput())
        mock_record.assert_called_once()

    async def test_bare_name_matches_the_qualified_activity_type(self, mock_next):
        """A v3 activity registers as "{app}:{task}" but a dev writes the task name;
        requiring the qualified form would silently collect nothing."""
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({"merge"})
        )
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET, _tracker(ContainerTrace(peak_memory_bytes=1))),
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo(
                activity_type="automation-engine:merge"
            )
            await interceptor.execute_activity(MockExecuteActivityInput())
        mock_record.assert_called_once()
        assert mock_record.call_args[0][0].activity_type == "automation-engine:merge"

    async def test_qualified_name_also_matches(self, mock_next):
        """Both spellings work, so same-named tasks can be disambiguated."""
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({"automation-engine:merge"})
        )
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET, _tracker(ContainerTrace(peak_memory_bytes=1))),
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo(
                activity_type="automation-engine:merge"
            )
            await interceptor.execute_activity(MockExecuteActivityInput())
        mock_record.assert_called_once()

    async def test_qualified_entry_does_not_match_another_app(self, mock_next):
        """The qualified form must stay a narrowing, not a no-op."""
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({"automation-engine:merge"})
        )
        with (
            patch(_ACTIVITY_TARGET) as mock_act,
            patch(_TRACKER_TARGET) as mock_tracker,
            patch(_RECORD_TARGET) as mock_record,
        ):
            mock_act.info.return_value = MockActivityInfo(
                activity_type="publish-app:merge"
            )
            await interceptor.execute_activity(MockExecuteActivityInput())
        mock_tracker.assert_not_called()
        mock_record.assert_not_called()

    async def test_a_failing_unselected_activity_still_propagates(self, mock_next):
        """The passthrough path must not change failure behaviour."""
        mock_next.execute_activity = AsyncMock(side_effect=ValueError("boom"))
        interceptor = _SizingActivityInboundInterceptor(
            mock_next, 1.0, frozenset({"merge"})
        )
        with patch(_ACTIVITY_TARGET) as mock_act:
            mock_act.info.return_value = MockActivityInfo(activity_type="other")
            with pytest.raises(ValueError, match="boom"):
                await interceptor.execute_activity(MockExecuteActivityInput())


class TestSizingSettings:
    def test_off_by_default(self, monkeypatch):
        """A version bump alone must change nothing on any tenant."""
        monkeypatch.delenv("APPLICATION_SDK_ENABLE_SIZING_TELEMETRY", raising=False)
        assert load_interceptor_settings().enable_sizing_telemetry is False

    def test_enabled_by_env(self, monkeypatch):
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_SIZING_TELEMETRY", "true")
        assert load_interceptor_settings().enable_sizing_telemetry is True

    def test_poll_seconds_default(self, monkeypatch):
        monkeypatch.delenv(
            "APPLICATION_SDK_SIZING_TELEMETRY_POLL_SECONDS", raising=False
        )
        assert load_interceptor_settings().sizing_telemetry_poll_seconds == 1.0

    def test_poll_seconds_override(self, monkeypatch):
        monkeypatch.setenv("APPLICATION_SDK_SIZING_TELEMETRY_POLL_SECONDS", "0.5")
        assert load_interceptor_settings().sizing_telemetry_poll_seconds == 0.5

    def test_garbage_poll_seconds_falls_back(self, monkeypatch):
        """A bad env value must not stop workers starting."""
        monkeypatch.setenv("APPLICATION_SDK_SIZING_TELEMETRY_POLL_SECONDS", "soon")
        assert load_interceptor_settings().sizing_telemetry_poll_seconds == 1.0

    def test_negative_poll_seconds_is_clamped(self, monkeypatch):
        monkeypatch.setenv("APPLICATION_SDK_SIZING_TELEMETRY_POLL_SECONDS", "-5")
        assert load_interceptor_settings().sizing_telemetry_poll_seconds == 0.0

    def test_activities_default_to_empty(self, monkeypatch):
        """Unset means nothing measured, never everything."""
        monkeypatch.delenv("APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES", raising=False)
        assert load_interceptor_settings().sizing_telemetry_activities == frozenset()

    def test_activities_are_parsed(self, monkeypatch):
        monkeypatch.setenv(
            "APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES", "merge,fetch_entities"
        )
        assert load_interceptor_settings().sizing_telemetry_activities == frozenset(
            {"merge", "fetch_entities"}
        )

    def test_activities_tolerate_helm_whitespace_and_empty_entries(self, monkeypatch):
        """Hand-edited in a values file, so stray whitespace has to work."""
        monkeypatch.setenv(
            "APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES", " merge , , transform ,"
        )
        assert load_interceptor_settings().sizing_telemetry_activities == frozenset(
            {"merge", "transform"}
        )

    def test_wildcard_is_parsed(self, monkeypatch):
        monkeypatch.setenv("APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES", "*")
        assert load_interceptor_settings().sizing_telemetry_activities == frozenset(
            {"*"}
        )
