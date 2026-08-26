"""Unit tests for the durable sizing sink."""

from __future__ import annotations

import asyncio
import gzip
import pathlib
from unittest.mock import MagicMock, patch

import orjson
import pytest

from application_sdk.constants import SIZING_FILE_NAME
from application_sdk.observability import sizing_sink
from application_sdk.observability.observability import (
    LOCAL_OBS_SUBDIR_MAP,
    OBSERVABILITY_S3_PREFIX_MAP,
)
from application_sdk.observability.sizing import SizingObservation
from application_sdk.observability.sizing_sink import (
    SIZING_SCHEMA_VERSION,
    SizingObservabilitySink,
    persist,
)


def _observation(**overrides) -> SizingObservation:
    base = {
        "activity_type": "automation-engine:merge",
        "task_queue": "atlan-automation-engine-heavy",
        "workflow_type": "AutomationWorkflow",
        "attempt": 1,
        "outcome": "OK",
        "duration_seconds": 47.2,
        "peak_memory_bytes": 6 * 1024**3,
        "peak_memory_fraction": 0.375,
        "peak_source": "poll",
        "memory_limit_bytes": 16 * 1024**3,
        "start_memory_bytes": 1024**3,
        "cpu_seconds": 78.4,
        "cpu_throttled_seconds": 12.1,
        "cpu_throttled_fraction": 0.31,
        "cpu_quota_cores": 2.0,
        "input_bytes": 2 * 1024**3,
        "input_file_count": 24,
        "input_basis": "reported",
        "started_at": 1755690000.0,
        "pod": "ae-heavy-7f9c",
        "concurrency_max": 1,
    }
    base.update(overrides)
    return SizingObservation(**base)


@pytest.fixture
def sink(tmp_path):
    sizing_sink._reset_for_testing()
    s = SizingObservabilitySink(
        batch_size=1000,
        flush_interval=3600,
        retention_days=1,
        cleanup_enabled=False,
        data_dir=str(tmp_path),
        file_name=SIZING_FILE_NAME,
    )
    yield s
    sizing_sink._reset_for_testing()


class TestSignalRouting:
    def test_sizing_has_its_own_s3_prefix(self):
        """Not the ``other`` fallback: it must be selectable across tenants."""
        assert "sizing" in OBSERVABILITY_S3_PREFIX_MAP
        assert OBSERVABILITY_S3_PREFIX_MAP["sizing"].endswith("/sizing")

    def test_local_and_remote_maps_agree(self):
        """Two separate maps drive the local dir and the remote key."""
        assert LOCAL_OBS_SUBDIR_MAP["sizing"].endswith("/sizing")
        assert set(LOCAL_OBS_SUBDIR_MAP) == set(OBSERVABILITY_S3_PREFIX_MAP)

    def test_signal_type_is_sizing(self, sink):
        """Drives the partition path, so a wrong answer buries the dataset."""
        assert sink._get_signal_type() == "sizing"


class TestStoreSinkGate:
    """The sizing sink must not be switched off by an unrelated signal's flag."""

    def test_sizing_ignores_the_shared_store_sink_flag(self, sink):
        """AE resolves ENABLE_OBSERVABILITY_STORE_SINK to False."""
        assert sink._store_sink_enabled() is True

    def test_other_signals_still_respect_it(self, tmp_path):
        """The override is scoped to sizing, not a loosening of the switch."""
        from application_sdk.constants import LOG_FILE_NAME
        from application_sdk.observability.observability import AtlanObservability

        class _Other(AtlanObservability):
            def process_record(self, record):
                return {}

            def export_record(self, record):
                return None

        other = _Other(
            batch_size=1,
            flush_interval=3600,
            retention_days=1,
            cleanup_enabled=False,
            data_dir=str(tmp_path),
            file_name=LOG_FILE_NAME,
        )
        with patch(
            "application_sdk.observability.observability.ENABLE_OBSERVABILITY_STORE_SINK",
            False,
        ):
            assert other._store_sink_enabled() is False

    @pytest.mark.asyncio
    async def test_a_flush_uploads_with_the_shared_flag_off(self, sink, tmp_path):
        """End to end: the flush must reach the object store, flag or no flag."""
        uploads: list[tuple[str, str]] = []

        async def fake_upload(remote_key, local_path, **kwargs):
            body = gzip.decompress(pathlib.Path(local_path).read_bytes()).decode()
            uploads.append((remote_key, body))

        with (
            patch(
                "application_sdk.observability.observability.ENABLE_OBSERVABILITY_STORE_SINK",
                False,
            ),
            patch("application_sdk.storage.upload_file", new=fake_upload),
        ):
            await sink._flush_records([sink.process_record(_observation())])

        assert uploads, "sizing flush uploaded nothing"
        remote_key, body = uploads[0]
        # Normalise separators: the partition path comes from os.path.join, so on
        # Windows these are backslashes. The assertion is about the signal landing
        # under its own prefix, not about which separator the host uses.
        key = remote_key.replace("\\", "/")
        assert "/sizing/" in key
        # Hive-partitioned on the execution's start, not the flush time.
        assert "year=" in key and "hour=" in key
        row = orjson.loads(body.splitlines()[0])
        assert row["activity_type"] == "automation-engine:merge"
        assert row["schema_version"] == SIZING_SCHEMA_VERSION
        assert row["concurrency_max"] == 1
        assert row["peak_per_input_byte"] == pytest.approx(3.0)


class TestProcessRecord:
    def test_carries_the_schema_version(self, sink):
        """Rows are read months later, mixed across SDK versions."""
        row = sink.process_record(_observation())
        assert row["schema_version"] == SIZING_SCHEMA_VERSION

    def test_stamps_app_and_deployment(self, sink):
        """Cross-tenant rows sit under one prefix; a row must name its origin."""
        row = sink.process_record(_observation())
        assert "app" in row
        assert "deployment" in row

    def test_flattens_the_measurements(self, sink):
        row = sink.process_record(_observation())
        assert row["activity_type"] == "automation-engine:merge"
        assert row["peak_memory_bytes"] == 6 * 1024**3
        assert row["cpu_throttled_fraction"] == 0.31
        assert row["input_basis"] == "reported"

    def test_precomputes_the_derived_ratios(self, sink):
        """Derived once here so every consumer computes them identically."""
        row = sink.process_record(_observation())
        assert row["peak_per_input_byte"] == pytest.approx(3.0)
        assert row["mean_cpu_cores"] == pytest.approx(78.4 / 47.2)

    def test_derived_ratios_are_none_when_undefined(self, sink):
        row = sink.process_record(_observation(input_bytes=None, cpu_seconds=None))
        assert row["peak_per_input_byte"] is None
        assert row["mean_cpu_cores"] is None

    def test_carries_the_baseline_and_the_unbiased_ratio(self, sink):
        """The delta columns are what a multiplier gets fitted on."""
        row = sink.process_record(_observation())
        assert row["start_memory_bytes"] == 1024**3
        assert row["peak_delta_bytes"] == 5 * 1024**3
        assert row["delta_per_input_byte"] == pytest.approx(2.5)
        # The biased ratio stays, so v3 rows remain comparable with v2 ones.
        assert row["peak_per_input_byte"] == pytest.approx(3.0)

    def test_delta_columns_are_none_without_a_baseline(self, sink):
        """A pre-fix row must read as "unknown", never as a zero-size baseline."""
        row = sink.process_record(_observation(start_memory_bytes=None))
        assert row["start_memory_bytes"] is None
        assert row["peak_delta_bytes"] is None
        assert row["delta_per_input_byte"] is None

    def test_carries_the_join_keys(self, sink):
        """pod + started_at + duration is how overlap is rebuilt at analysis time."""
        row = sink.process_record(_observation())
        assert row["pod"] == "ae-heavy-7f9c"
        assert row["started_at"] == 1755690000.0
        assert row["duration_seconds"] == 47.2

    def test_attributability_is_written_not_derived(self, sink):
        """A consumer that forgets the flag would pool activity and pod peaks."""
        assert sink.process_record(_observation())["is_attributable"] is True
        row = sink.process_record(_observation(concurrency_max=6))
        assert row["concurrency_max"] == 6
        assert row["is_attributable"] is False

    def test_export_record_is_a_no_op(self, sink):
        """Histograms come from record_observation, not from the batching sink."""
        assert sink.export_record(_observation()) is None


class TestPersist:
    def test_buffers_the_observation(self, tmp_path):
        sizing_sink._reset_for_testing()
        fake = MagicMock()
        with patch.object(sizing_sink, "get_sink", return_value=fake):
            persist(_observation())
        fake.add_record.assert_called_once()

    def test_never_raises_when_the_sink_is_broken(self):
        """Called from an activity's finally; must cost the record, not the run."""
        with patch.object(
            sizing_sink, "get_sink", side_effect=RuntimeError("no store")
        ):
            persist(_observation())  # must not raise

    def test_never_raises_when_add_record_fails(self):
        fake = MagicMock()
        fake.add_record.side_effect = RuntimeError("buffer full")
        with patch.object(sizing_sink, "get_sink", return_value=fake):
            persist(_observation())  # must not raise

    def test_unavailable_sink_is_tolerated(self):
        with patch.object(sizing_sink, "get_sink", return_value=None):
            persist(_observation())  # must not raise


class TestGetSink:
    def test_is_cached(self, tmp_path):
        """One sink per process — a second would double-buffer and double-upload."""
        sizing_sink._reset_for_testing()
        with patch.object(
            sizing_sink, "get_observability_dir", return_value=str(tmp_path)
        ):
            first = sizing_sink.get_sink()
            second = sizing_sink.get_sink()
        assert first is second
        sizing_sink._reset_for_testing()

    def test_returns_none_when_construction_fails(self):
        """A worker whose observability dir is unwritable must still run activities."""
        sizing_sink._reset_for_testing()
        with patch.object(
            sizing_sink,
            "SizingObservabilitySink",
            side_effect=OSError("read-only fs"),
        ):
            assert sizing_sink.get_sink() is None
        sizing_sink._reset_for_testing()


class TestRecordObservationPersists:
    def test_record_observation_reaches_the_sink(self):
        """The wiring: histograms and the durable row come from the same call."""
        from application_sdk.observability import sizing as sizing_module

        with (
            patch("application_sdk.observability.sizing_sink.persist") as mock_persist,
            patch(
                "application_sdk.observability.sizing.create_histogram",
                return_value=MagicMock(),
            ),
        ):
            sizing_module._INSTRUMENTS.clear()
            sizing_module.record_observation(_observation())
            sizing_module._INSTRUMENTS.clear()

        mock_persist.assert_called_once()

    def test_nothing_measured_is_not_persisted(self):
        """An all-null row must not enter the dataset tiers are fitted from."""
        from application_sdk.observability import sizing as sizing_module

        with patch("application_sdk.observability.sizing_sink.persist") as mock_persist:
            sizing_module.record_observation(
                _observation(peak_memory_bytes=None, cpu_seconds=None)
            )
        mock_persist.assert_not_called()


class TestTheFlushActuallyHappens:
    """The bug this class exists for: a full day of collection wrote nothing.
    ``add_record`` only evaluates its flush condition when a record ARRIVES.
    """

    @pytest.mark.asyncio
    async def test_a_periodic_flush_task_is_started(self, tmp_path):
        sizing_sink._reset_for_testing()
        started = []
        real = SizingObservabilitySink._periodic_flush

        async def spy(self):
            started.append(True)

        with (
            patch.object(SizingObservabilitySink, "_periodic_flush", spy),
            patch.object(
                sizing_sink, "get_observability_dir", return_value=str(tmp_path)
            ),
        ):
            sizing_sink.get_sink()
            await asyncio.sleep(0)  # let the created task run
        assert (
            started
        ), "no periodic flush task was started — the buffer would never drain"
        sizing_sink._reset_for_testing()
        SizingObservabilitySink._periodic_flush = real

    @pytest.mark.asyncio
    async def test_one_record_then_shutdown_is_not_lost(self, sink):
        """The exact shape that lost a day of data."""
        flushed: list[list] = []

        async def capture(records):
            flushed.append(records)

        sizing_sink._sink = sink
        with patch.object(sink, "_flush_records", capture):
            sink.add_record(_observation())  # a single record, no second one
            assert not flushed, "premature flush would make this test vacuous"
            await sizing_sink.drain()
        sizing_sink._reset_for_testing()

        assert flushed, "the single buffered row was dropped on shutdown"
        assert flushed[0][0]["activity_type"] == "automation-engine:merge"

    @pytest.mark.asyncio
    async def test_drain_is_safe_with_no_sink(self):
        sizing_sink._reset_for_testing()
        await sizing_sink.drain()  # must not raise

    @pytest.mark.asyncio
    async def test_drain_never_raises(self, sink):
        """Runs on the shutdown path; a failure must not block termination."""
        sizing_sink._sink = sink
        with patch.object(
            sink, "_flush_buffer", side_effect=RuntimeError("store gone")
        ):
            await sizing_sink.drain()  # must not raise
        sizing_sink._reset_for_testing()

    @pytest.mark.asyncio
    async def test_flush_logs_at_info(self, sink, tmp_path):
        """'Is it writing?' must be answerable from logs, not by exec-ing into a pod.
        The base class logs its success at DEBUG, which every deployment filters.
        """

        async def fake_upload(remote_key, local_path, **kw):
            return None

        with (
            patch("application_sdk.storage.upload_file", new=fake_upload),
            patch.object(sizing_sink, "logger") as mock_log,
        ):
            await sink._flush_records([sink.process_record(_observation())])
        msgs = " ".join(str(c) for c in mock_log.info.call_args_list)
        assert "flushed" in msgs
