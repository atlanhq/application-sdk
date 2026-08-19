"""Unit tests for the durable sizing sink."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
        "cpu_seconds": 78.4,
        "cpu_throttled_seconds": 12.1,
        "cpu_throttled_fraction": 0.31,
        "cpu_quota_cores": 2.0,
        "input_bytes": 2 * 1024**3,
        "input_file_count": 24,
        "input_basis": "reported",
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
        """Two separate maps drive the local dir and the remote key.

        A signal in only one of them writes to ``other/`` on disk while uploading to
        ``sizing/`` — consistent enough to work, confusing enough to lose.
        """
        assert LOCAL_OBS_SUBDIR_MAP["sizing"].endswith("/sizing")
        assert set(LOCAL_OBS_SUBDIR_MAP) == set(OBSERVABILITY_S3_PREFIX_MAP)

    def test_signal_type_is_sizing(self, sink):
        """Drives the partition path, so a wrong answer buries the dataset."""
        assert sink._get_signal_type() == "sizing"


class TestProcessRecord:
    def test_carries_the_schema_version(self, sink):
        """Rows are read months later, mixed across SDK versions."""
        row = sink.process_record(_observation())
        assert row["schema_version"] == SIZING_SCHEMA_VERSION

    def test_stamps_app_and_deployment(self, sink):
        """Cross-tenant rows sit under one prefix; a row must name its origin.

        Pooling tenants blindly would be wrong anyway — a tenant's data volume is
        the very thing being measured.
        """
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
                "application_sdk.observability.sizing._otel_metrics.get_meter",
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
